"""Gemini resumable file upload with bounded processing wait and cleanup."""

import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from paoding.core.errors import ConfigurationError, TransientError


@contextmanager
def gemini_files(gateway, provider, media):
    parts, uploaded = [], []
    base = provider.base_url.rstrip("/")
    headers = gateway.auth_headers(provider)
    base_parts = urlsplit(base)
    upload_endpoint = urlunsplit(
        (base_parts.scheme, base_parts.netloc, "/upload" + base_parts.path + "/files", "", "")
    )
    from paoding.models.gateway import media_data

    try:
        with gateway.client() as client:
            for path in media:
                path = Path(path)
                if path.stat().st_size <= 15 * 1024 * 1024:
                    mime, data = media_data(path)
                    parts.append({"inline_data": {"mime_type": mime, "data": data}})
                    continue
                mime = "video/quicktime" if path.suffix == ".mov" else "video/mp4"
                response = client.post(
                    upload_endpoint,
                    headers={
                        **headers,
                        "X-Goog-Upload-Protocol": "resumable",
                        "X-Goog-Upload-Command": "start",
                        "X-Goog-Upload-Header-Content-Length": str(path.stat().st_size),
                        "X-Goog-Upload-Header-Content-Type": mime,
                    },
                    json={"file": {"display_name": path.name}},
                )
                response.raise_for_status()
                upload_url = response.headers["x-goog-upload-url"]
                target = urlsplit(upload_url)
                if target.scheme != "https" or target.netloc != base_parts.netloc:
                    raise ConfigurationError("Unexpected Gemini upload origin")
                with path.open("rb") as stream:
                    upload_headers = {
                        "X-Goog-Upload-Offset": "0",
                        "X-Goog-Upload-Command": "upload, finalize",
                        "Content-Length": str(path.stat().st_size),
                    }
                    if provider.protocol == "gemini_oauth":
                        upload_headers.update(headers)
                    response = client.post(
                        upload_url,
                        headers=upload_headers,
                        content=stream,
                        timeout=gateway.settings.gemini_file_timeout,
                    )
                response.raise_for_status()
                file = response.json()["file"]
                name = file["name"]
                if not name.startswith("files/") or ".." in name or "?" in name:
                    raise ConfigurationError("Unexpected Gemini file identifier")
                uploaded.append(name)
                deadline = time.monotonic() + gateway.settings.gemini_file_timeout
                while file.get("state") == "PROCESSING" and time.monotonic() < deadline:
                    time.sleep(1)
                    response = client.get(base + "/" + name, headers=headers)
                    response.raise_for_status()
                    file = response.json()
                if file.get("state") != "ACTIVE":
                    raise TransientError("Gemini file processing did not complete")
                parts.append({"file_data": {"mime_type": mime, "file_uri": file["uri"]}})
        yield parts
    finally:
        for name in uploaded:
            try:
                with gateway.client() as client:
                    client.delete(base + "/" + name, headers=headers)
            except Exception:
                # Cleanup is best-effort; it must not mask an inference result. Gemini expires uploads.
                import logging

                logging.getLogger(__name__).warning("Gemini temporary upload cleanup failed")
