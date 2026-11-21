import io
import zipfile

import httpx
import pytest
from pydantic import SecretStr

from paoding.core.errors import SourceError
from paoding.ingestion.documents import DocumentExtractor, WebReader
from paoding.integrations.http import ServiceHTTP


def document_zip(name="full.md"):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr(name, "# Extracted\n\n| Column | Value |\n|---|---|\n| A | 1 |")
        archive.writestr("images/figure.png", b"png-data")
    return data.getvalue()


def test_mineru_upload_poll_archive_and_cache(rt, tmp_path):
    rt.settings.mineru_token = SecretStr("test")
    file = tmp_path / "presentation.pptx"
    file.write_bytes(b"presentation")
    requests = []

    def handle(request):
        requests.append(request)
        if request.url.path.endswith("file-urls/batch"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "batch_id": "batch-1",
                        "file_urls": ["https://uploads.example/document"],
                    },
                },
            )
        if request.method == "PUT":
            return httpx.Response(200)
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "extract_result": [
                        {"state": "done", "full_zip_url": "https://cdn.example/result.zip"}
                    ]
                },
            },
        )

    class Fetcher:
        def fetch(self, url):
            return document_zip(), "application/zip", url

    extractor = DocumentExtractor(
        rt.settings, ServiceHTTP(transport=httpx.MockTransport(handle)), Fetcher()
    )
    text, assets = extractor.extract(file)
    assert "| A | 1 |" in text
    assert any(path.endswith("figure.png") for path in assets)
    assert len(requests) == 3
    assert extractor.extract(file)[0] == text
    assert len(requests) == 3  # Retry/repeated import reuses the completed artifact.


@pytest.mark.parametrize("name", ["../escape.md", "/absolute.md"])
def test_document_archive_cannot_escape_root(rt, tmp_path, name):
    with pytest.raises(SourceError, match="Unsafe path"):
        DocumentExtractor(rt.settings).unpack(document_zip(name), tmp_path / "input.pdf")


def test_document_archive_requires_forward_slashes(rt, tmp_path):
    # Supply the stored spelling directly so the ZIP writer produces the same input on every OS.
    member = zipfile.ZipInfo()
    member.filename = r"folder\notes.md"
    with pytest.raises(SourceError, match="Unsafe path"):
        DocumentExtractor(rt.settings).unpack(document_zip(member), tmp_path / "input.pdf")


def test_web_readers_real_request_contracts(rt):
    calls = []

    def handle(request):
        calls.append(request)
        if request.method == "GET":
            return httpx.Response(200, text="# Jina article")
        return httpx.Response(
            200, json={"success": True, "data": {"markdown": "# Firecrawl article"}}
        )

    reader = WebReader(rt.settings, ServiceHTTP(transport=httpx.MockTransport(handle)))
    rt.settings.webpage_reader = "jina"
    assert reader.read("https://example.com") == "# Jina article"
    rt.settings.webpage_reader = "firecrawl"
    rt.settings.firecrawl_token = SecretStr("secret")
    assert reader.read("https://example.com") == "# Firecrawl article"
    assert calls[0].url.host == "r.jina.ai"
    assert calls[1].url.path.endswith("/scrape")
