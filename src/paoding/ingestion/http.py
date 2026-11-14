import ipaddress
import socket
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from paoding.core.errors import SourceError, TransientError
from paoding.ingestion.urls import identify_url


class SafeFetcher:
    """Validate each hop and connect to the validated IP, preserving Host and TLS SNI.

    Environment proxies are intentionally disabled to prevent DNS rebinding through a proxy.
    """

    def __init__(self, max_bytes):
        self.max_bytes = max_bytes

    def fetch(self, url):
        for _ in range(6):
            source = identify_url(url)
            parts = urlsplit(source.url)
            port = parts.port or (443 if parts.scheme == "https" else 80)
            try:
                addresses = list(
                    dict.fromkeys(
                        info[4][0]
                        for info in socket.getaddrinfo(
                            parts.hostname, port, type=socket.SOCK_STREAM
                        )
                    )
                )
            except OSError as exc:
                raise TransientError("Source DNS lookup failed") from exc
            if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
                raise SourceError("Private, loopback and reserved source addresses are not allowed")
            ip = addresses[0]
            netloc = f"[{ip}]:{port}" if ":" in ip else f"{ip}:{port}"
            target = urlunsplit((parts.scheme, netloc, parts.path, parts.query, ""))
            try:
                with httpx.Client(timeout=httpx.Timeout(30, connect=5), trust_env=False) as client:
                    with client.stream(
                        "GET",
                        target,
                        headers={"Host": parts.netloc, "User-Agent": "PaoDing/0.1"},
                        extensions={"sni_hostname": parts.hostname},
                    ) as response:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            if not response.headers.get("location"):
                                raise SourceError("Redirect without Location")
                            url = urljoin(source.url, response.headers["location"])
                            continue
                        if response.status_code == 429 or response.status_code >= 500:
                            raise TransientError(
                                f"Source temporarily unavailable (HTTP {response.status_code})"
                            )
                        if response.status_code >= 400:
                            raise SourceError(
                                f"Source rejected request (HTTP {response.status_code})"
                            )
                        chunks, size = [], 0
                        for chunk in response.iter_bytes():
                            size += len(chunk)
                            if size > self.max_bytes:
                                raise SourceError("Source exceeds configured download limit")
                            chunks.append(chunk)
                        return (
                            b"".join(chunks),
                            response.headers.get("content-type", ""),
                            source.url,
                        )
            except httpx.HTTPError as exc:
                raise TransientError("Source network request failed") from exc
        raise SourceError("Too many source redirects")
