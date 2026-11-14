"""HTTP for administrator-configured services (distinct from untrusted source fetching)."""

import httpx

from paoding.core.errors import ConfigurationError, SourceError, TransientError


def check_response(response):
    if response.status_code in {401, 403}:
        raise ConfigurationError("Service authentication or scope is invalid")
    if response.status_code == 429 or response.status_code >= 500:
        raise TransientError(f"Service temporarily unavailable (HTTP {response.status_code})")
    if response.status_code >= 400:
        raise SourceError(f"Service rejected request (HTTP {response.status_code})")
    return response


class ServiceHTTP:
    def __init__(self, timeout=60, transport=None):
        self.timeout, self.transport = timeout, transport

    def request(self, method, url, **kwargs):
        try:
            with httpx.Client(
                timeout=self.timeout, transport=self.transport, trust_env=False
            ) as client:
                return check_response(client.request(method, url, **kwargs))
        except httpx.HTTPError as exc:
            raise TransientError("Service network request failed") from exc

    def json(self, method, url, **kwargs):
        return self.request(method, url, **kwargs).json()

    def download(self, url, max_bytes, **kwargs):
        try:
            with httpx.Client(
                timeout=self.timeout, transport=self.transport, trust_env=False
            ) as client:
                with client.stream("GET", url, **kwargs) as response:
                    check_response(response)
                    chunks, size = [], 0
                    for chunk in response.iter_bytes(65536):
                        size += len(chunk)
                        if size > max_bytes:
                            raise SourceError("Service attachment exceeds download limit")
                        chunks.append(chunk)
                    return b"".join(chunks), response.headers.get("content-type", "")
        except httpx.HTTPError as exc:
            raise TransientError("Attachment transfer failed") from exc
