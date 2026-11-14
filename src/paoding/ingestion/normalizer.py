import hashlib
import io
from pathlib import Path
from urllib.parse import urlsplit

import colorgram
import httpx
from bs4 import BeautifulSoup
from markdownify import markdownify
from PIL import Image, UnidentifiedImageError
from pypdf import PdfReader

from paoding.core.errors import ConfigurationError, SourceError, TransientError
from paoding.domain.schemas import (
    CollectorItem,
    CollectorPage,
    ColorSwatch,
    Modality,
    NormalizedContent,
)
from paoding.ingestion.http import SafeFetcher
from paoding.ingestion.urls import identify_url

SOCIAL_PLATFORMS = {"xiaohongshu", "douyin", "wechat", "wechat_video", "youtube", "x", "pinterest"}
SUPPORTED_EXTENSIONS = {
    ".md",
    ".txt",
    ".html",
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".mp4",
    ".mov",
    ".webm",
    ".mkv",
    ".mp3",
    ".wav",
    ".m4a",
    ".flac",
    ".ogg",
    ".amr",
    ".opus",
    ".ppt",
    ".pptx",
    ".doc",
    ".docx",
}


class CollectorBridge:
    """Explicit vendor-neutral contract. No invented TikHub or WorkBuddy endpoints."""

    def __init__(self, settings):
        self.settings = settings

    def request(self, route, payload):
        if not self.settings.collector_url:
            raise ConfigurationError(
                "This platform requires PAODING_COLLECTOR_URL or a local file export"
            )
        headers = {"Authorization": f"Bearer {self.settings.collector_token.get_secret_value()}"}
        try:
            with httpx.Client(timeout=60, trust_env=False) as client:
                result = client.post(
                    self.settings.collector_url.rstrip("/") + route, json=payload, headers=headers
                )
                if result.status_code in {401, 403, 404}:
                    raise ConfigurationError(
                        "Collector authentication or endpoint configuration is invalid"
                    )
                result.raise_for_status()
                return result.json()
        except httpx.HTTPError as exc:
            raise TransientError("Collector bridge is unavailable") from exc

    def resolve(self, url):
        return CollectorItem.model_validate(self.request("/v1/resolve", {"url": url}))

    def account_items(self, url, cursor, cold_start):
        return CollectorPage.model_validate(
            self.request(
                "/v1/accounts/items",
                {
                    "url": url,
                    "cursor": cursor,
                    "limit": 20 if cold_start else 100,
                    "sort": "engagement_desc" if cold_start else "published_asc",
                },
            )
        )


class Normalizer:
    def __init__(self, settings, fetcher=None, collector=None):
        self.settings = settings
        self.fetcher = fetcher or SafeFetcher(settings.max_download_bytes)
        from paoding.ingestion.collectors import NativeCollectors
        from paoding.ingestion.documents import DocumentExtractor

        self.collector = collector or NativeCollectors(settings, CollectorBridge(settings))
        self.documents = DocumentExtractor(settings)

    def _store(self, content, extension):
        digest = hashlib.sha256(content).hexdigest()
        path = self.settings.data_dir.resolve() / "media" / f"{digest}{extension}"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            # Content-addressed immutable files; duplicate writes contain identical bytes.
            from paoding.storage.vault import atomic_write

            atomic_write(path, content)
        return path

    def local(self, path: Path):
        path = path.resolve()
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            raise SourceError("Unsupported local file; use Markdown, text, PDF, image or MP4/MOV")
        if path.stat().st_size > self.settings.max_download_bytes:
            raise SourceError("Local file exceeds configured size limit")
        return self.from_bytes(path.read_bytes(), "", path.as_uri(), "local", path.name)

    def normalize(self, payload):
        if payload.get("local_paths"):
            parts = []
            for name in payload["local_paths"]:
                path = Path(name).resolve()
                if not path.is_relative_to((self.settings.data_dir / "media").resolve()):
                    raise SourceError("Export media must be inside the managed media directory")
                parts.append(self.local(path))
            content = next(
                (part for part in parts if part.modality == Modality.VIDEO),
                next((part for part in parts if part.modality == Modality.IMAGE), parts[0]),
            )
            content = content.model_copy(deep=True)
            if content.modality == Modality.IMAGE:
                content.media_paths = [
                    path
                    for part in parts
                    if part.modality == Modality.IMAGE
                    for path in part.media_paths
                ]
                if len(content.media_paths) > 1:
                    content.limitations.append(
                        "Measured color palette describes the first image; layout analysis can observe the full gallery"
                    )
            content.text = (
                payload.get("inline_text", "")
                + "\n\n"
                + "\n\n".join(part.text for part in parts if part.text)
            )[: self.settings.max_text_chars]
            content.source_url, content.platform = (
                payload["source_url"],
                payload.get("platform", "local"),
            )
            content.title = payload.get("title", content.title)
            content.original_media_paths = [Path(name) for name in payload["local_paths"]]
            return content
        if payload.get("inline_text"):
            return NormalizedContent(
                source_url=payload.get("source_url", "bot:message"),
                platform=payload.get("platform", "local"),
                title=payload.get("title", "转发内容"),
                text=payload["inline_text"][: self.settings.max_text_chars],
                modality="text",
            )
        if "local_path" in payload:
            # Only trusted CLI/watcher can enqueue paths. Enforce our managed snapshot root too.
            path = Path(payload["local_path"]).resolve()
            if not path.is_relative_to((self.settings.data_dir / "media").resolve()):
                raise SourceError("Local job must reference an ingested media snapshot")
            content = self.local(path)
            content.source_url = payload.get("source_url", content.source_url)
            return content
        source = identify_url(payload["url"])
        if source.platform in SOCIAL_PLATFORMS or payload.get("collected_item"):
            item = (
                CollectorItem.model_validate(payload["collected_item"])
                if payload.get("collected_item")
                else self.collector.resolve(source.url)
            )
            paths, palette = [], []
            for local_path in item.local_media_paths:
                local_path = local_path.resolve()
                if not local_path.is_relative_to((self.settings.data_dir / "media").resolve()):
                    raise SourceError("Collector file must be inside the managed media directory")
                piece = self.local(local_path)
                paths.extend(piece.media_paths)
                if not palette:
                    palette = piece.palette
            for media_url in item.media_urls:
                data, mime, final_url = self.fetcher.fetch(str(media_url))
                piece = self.from_bytes(data, mime, final_url, source.platform, item.title)
                paths.extend(piece.media_paths)
                if not palette:
                    palette = piece.palette
            if item.modality != Modality.TEXT and not paths:
                raise SourceError("Collector returned no media for a multimodal item")
            return NormalizedContent(
                source_url=source.url,
                platform=source.platform,
                title=item.title or source.url,
                modality=item.modality,
                text=item.text[: self.settings.max_text_chars],
                media_paths=paths,
                palette=palette,
                metrics=item.metrics,
                creator_id=payload.get("creator_id"),
                limitations=item.limitations,
                published_at=item.published_at,
                audio_paths=piece.audio_paths if item.modality == Modality.VIDEO and paths else [],
                duration_seconds=piece.duration_seconds
                if item.modality == Modality.VIDEO and paths
                else None,
                video_segments=piece.video_segments
                if item.modality == Modality.VIDEO and paths
                else [],
                original_media_paths=piece.original_media_paths
                if item.modality == Modality.VIDEO and paths
                else [],
            )
        data, mime, final_url = self.fetcher.fetch(source.url)
        # Short links may resolve to a profile/social site; require an explicit collector, not login HTML.
        final_source = identify_url(final_url)
        if final_source.platform in SOCIAL_PLATFORMS:
            return self.normalize({**payload, "url": final_url})
        content = self.from_bytes(data, mime, final_url, source.platform, final_source.url)
        if (
            content.modality == Modality.TEXT
            and "html" in mime
            and self.settings.webpage_reader != "local"
        ):
            from paoding.ingestion.documents import WebReader

            content.text = WebReader(self.settings).read(final_url)[: self.settings.max_text_chars]
            content.media_paths.append(self._store(content.text.encode(), ".md"))
        return content

    def from_bytes(self, data, mime, source_url, platform, title):
        suffix = Path(urlsplit(source_url).path).suffix.lower()
        content = NormalizedContent(
            source_url=source_url, platform=platform, title=title, modality=Modality.TEXT
        )
        if (
            mime.startswith("image/")
            or suffix in {".jpg", ".jpeg", ".png", ".webp"}
            or data.startswith((b"\xff\xd8\xff", b"\x89PNG"))
        ):
            try:
                with Image.open(io.BytesIO(data)) as image:
                    image.load()
                    image = image.convert("RGB")
                    palette = colorgram.extract(image, 6)
                    image.thumbnail((2048, 2048))
                    output = io.BytesIO()
                    image.save(output, format="JPEG", quality=90)
                    path = self._store(output.getvalue(), ".jpg")
            except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
                raise SourceError("Invalid or oversized image") from exc
            content.modality, content.media_paths = Modality.IMAGE, [path]
            original_suffix = (
                suffix
                if suffix in {".jpg", ".jpeg", ".png", ".webp"}
                else ".png"
                if data.startswith(b"\x89PNG")
                else ".jpg"
            )
            content.original_media_paths = [self._store(data, original_suffix)]
            content.palette = [
                ColorSwatch(
                    hex=f"#{c.rgb.r:02X}{c.rgb.g:02X}{c.rgb.b:02X}", proportion=c.proportion
                )
                for c in palette
            ]
        elif (
            mime.startswith("video/")
            or suffix in {".mp4", ".mov", ".webm", ".mkv"}
            or (data[4:8] == b"ftyp" and not mime.startswith("audio/") and suffix != ".m4a")
        ):
            content.modality = Modality.VIDEO
            original = self._store(data, suffix if suffix in {".mov", ".webm", ".mkv"} else ".mp4")
            from paoding.media.processing import MediaProcessor

            processed = MediaProcessor(self.settings).prepare_video(original)
            content.media_paths = [Path(processed["video"])]
            content.original_media_paths = [original]
            content.audio_paths = [Path(processed["audio"])] if processed["audio"] else []
            content.duration_seconds = processed["duration"]
            content.video_segments = processed["segments"]
            content.limitations.append(
                f"Original archived at {original}; analysis copy normalized to at most 720p"
            )
        elif mime.startswith("audio/") or suffix in {
            ".mp3",
            ".wav",
            ".m4a",
            ".flac",
            ".ogg",
            ".amr",
            ".opus",
        }:
            original = self._store(data, suffix or ".mp3")
            content.media_paths = [original]
            content.audio_paths = [original]
            content.text = "[待转写音频]"
            return content
        elif suffix in {".ppt", ".pptx", ".doc", ".docx"} or (
            (suffix == ".pdf" or data.startswith(b"%PDF")) and self.documents.enabled()
        ):
            original = self._store(data, suffix or ".pdf")
            content.text, assets = self.documents.extract(original)
            content.media_paths = [original, *[Path(asset) for asset in assets]]
            content.limitations.append(
                "Document structure extracted by MinerU; generated interpretations remain model inferences"
            )
        elif mime.startswith("application/pdf") or suffix == ".pdf" or data.startswith(b"%PDF"):
            try:
                reader = PdfReader(io.BytesIO(data))
                parts, length = [], 0
                for page in reader.pages[:300]:
                    text = page.extract_text() or ""
                    parts.append(text)
                    length += len(text)
                    if length > self.settings.max_text_chars:
                        break
                content.text = "\n\n".join(parts)
            except Exception as exc:
                raise SourceError(
                    "PDF parsing failed; export with MinerU for complex/scanned PDFs"
                ) from exc
            content.media_paths = [self._store(data, ".pdf")]
            content.limitations.append(
                "PDF text extraction only; page layout and charts are not analyzed"
            )
        else:
            if b"\x00" in data[:4096]:
                raise SourceError("Unsupported binary source")
            try:
                text = data.decode("utf-8-sig")
            except UnicodeDecodeError as exc:
                raise SourceError("Text source must be UTF-8") from exc
            if "html" in mime or suffix == ".html" or "<html" in text[:2000].lower():
                soup = BeautifulSoup(text, "html.parser")
                if soup.title and soup.title.string:
                    content.title = soup.title.string.strip()
                for element in soup(["script", "style", "nav", "footer", "noscript"]):
                    element.decompose()
                text = markdownify(str(soup.find("article") or soup.find("main") or soup))
            content.text = text.strip()
        if len(content.text) > self.settings.max_text_chars:
            content.limitations.append("Source text truncated at configured character limit")
            content.text = content.text[: self.settings.max_text_chars]
        if content.modality == Modality.TEXT:
            if not content.text.strip():
                raise SourceError(
                    "No readable source text; OCR/collector integration may be required"
                )
            if not content.media_paths:
                content.media_paths = [self._store(content.text.encode(), ".md")]
        return content
