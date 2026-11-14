import hashlib
import io
import json
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath

from paoding.core.errors import ConfigurationError, SourceError, TransientError
from paoding.ingestion.http import SafeFetcher
from paoding.integrations.http import ServiceHTTP
from paoding.storage.vault import atomic_write


class DocumentExtractor:
    def __init__(self, settings, http=None, fetcher=None):
        self.settings = settings
        self.http = http or ServiceHTTP(timeout=120)
        self.fetcher = fetcher or SafeFetcher(settings.max_download_bytes)

    def enabled(self):
        return self.settings.mineru_mode in {"cloud", "local"} or (
            self.settings.mineru_mode == "auto"
            and bool(self.settings.mineru_token.get_secret_value())
        )

    def extract(self, path):
        path = Path(path)
        if self.settings.mineru_mode == "local":
            return self.local(path)
        if not self.settings.mineru_token.get_secret_value():
            raise ConfigurationError(
                "Complex documents require PAODING_MINERU_TOKEN or mineru_mode=local"
            )
        return self.cloud(path)

    def local(self, path):
        import shutil

        if not shutil.which(self.settings.mineru_command):
            raise ConfigurationError("Install MinerU or configure its cloud token")
        with tempfile.TemporaryDirectory() as temp:
            try:
                result = subprocess.run(
                    [self.settings.mineru_command, "-p", str(path), "-o", temp],
                    capture_output=True,
                    timeout=self.settings.document_timeout,
                )
            except subprocess.TimeoutExpired as exc:
                raise TransientError("Local MinerU extraction timed out") from exc
            if result.returncode:
                raise SourceError("Local MinerU could not extract this document")
            markdowns = sorted(Path(temp).rglob("*.md"))
            if not markdowns:
                raise SourceError("MinerU returned no Markdown")
            # Preserve the extracted figure tree, using the same path validation as cloud archives.
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as archive:
                for file in Path(temp).rglob("*"):
                    if file.is_file() and not file.is_symlink():
                        archive.write(file, file.relative_to(temp).as_posix())
            return self.unpack(buffer.getvalue(), path)

    def cloud(self, path):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        state_path = self.settings.data_dir / "extract-jobs" / f"{digest}.json"
        headers = {"Authorization": f"Bearer {self.settings.mineru_token.get_secret_value()}"}
        base = self.settings.mineru_url.rstrip("/")
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        if state.get("markdown_path") and Path(state["markdown_path"]).exists():
            return Path(state["markdown_path"]).read_text(encoding="utf-8"), state["assets"]
        if not state.get("batch_id"):
            response = self.http.json(
                "POST",
                base + "/file-urls/batch",
                headers=headers,
                json={"files": [{"name": path.name, "data_id": digest}], "model_version": "vlm"},
            )
            if response.get("code") != 0:
                raise SourceError("MinerU rejected document upload request")
            data = response["data"]
            upload = data["file_urls"][0]
            if not upload.startswith("https://"):
                raise SourceError("MinerU returned an invalid upload URL")
            with path.open("rb") as stream:
                self.http.request("PUT", upload, content=stream)
            state = {"batch_id": data["batch_id"]}
            atomic_write(state_path, json.dumps(state).encode())
        deadline = time.monotonic() + self.settings.document_timeout
        while time.monotonic() < deadline:
            data = self.http.json(
                "GET", base + f"/extract-results/batch/{state['batch_id']}", headers=headers
            )
            if data.get("code") != 0:
                raise TransientError("MinerU task status unavailable")
            items = data["data"].get("extract_result", [])
            if items and items[0].get("state") == "failed":
                state_path.unlink(missing_ok=True)
                raise SourceError("MinerU document extraction failed")
            if items and items[0].get("state") == "done":
                archive, _, _ = self.fetcher.fetch(items[0]["full_zip_url"])
                markdown, assets = self.unpack(archive, path)
                md_path = self.settings.data_dir / "extract-jobs" / f"{digest}.md"
                atomic_write(md_path, markdown.encode())
                atomic_write(
                    state_path,
                    json.dumps(
                        {**state, "markdown_path": str(md_path.resolve()), "assets": assets}
                    ).encode(),
                )
                return markdown, assets
            time.sleep(2)
        raise TransientError(
            "MinerU processing is still pending; retry will resume the existing batch"
        )

    def unpack(self, data, source):
        root = self.settings.data_dir.resolve() / "documents" / hashlib.sha256(data).hexdigest()
        texts, assets, expanded = [], [], 0
        try:
            archive = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as exc:
            raise SourceError("MinerU returned an invalid archive") from exc
        with archive:
            for info in archive.infolist():
                path = PurePosixPath(info.filename)
                if path.is_absolute() or ".." in path.parts or "\\" in info.filename:
                    raise SourceError("Unsafe path in extraction archive")
                expanded += info.file_size
                if expanded > self.settings.max_download_bytes * 3:
                    raise SourceError("Extraction archive exceeds expanded size limit")
                if info.is_dir():
                    continue
                if path.suffix.lower() not in {".md", ".json", ".png", ".jpg", ".jpeg", ".webp"}:
                    continue
                target = root.joinpath(*path.parts)
                content = archive.read(info)
                atomic_write(target, content)
                assets.append(str(target))
                if path.suffix.lower() == ".md":
                    texts.append(content.decode("utf-8"))
        if not texts:
            raise SourceError("Extraction archive has no Markdown document")
        return "\n\n".join(texts), assets


class WebReader:
    def __init__(self, settings, http=None):
        self.settings, self.http = settings, http or ServiceHTTP(timeout=90)

    def read(self, url):
        # The service only receives a URL after SafeFetcher has validated and fetched it.
        if self.settings.webpage_reader == "jina":
            headers = {"X-Return-Format": "markdown"}
            if self.settings.jina_token.get_secret_value():
                headers["Authorization"] = f"Bearer {self.settings.jina_token.get_secret_value()}"
            return self.http.request("GET", "https://r.jina.ai/" + url, headers=headers).text
        if not self.settings.firecrawl_token.get_secret_value():
            raise ConfigurationError("PAODING_FIRECRAWL_TOKEN is required")
        data = self.http.json(
            "POST",
            self.settings.firecrawl_url.rstrip("/") + "/scrape",
            headers={"Authorization": f"Bearer {self.settings.firecrawl_token.get_secret_value()}"},
            json={"url": url, "formats": ["markdown"]},
        )
        if not data.get("success") or not data.get("data", {}).get("markdown"):
            raise SourceError("Firecrawl returned no Markdown")
        return data["data"]["markdown"]
