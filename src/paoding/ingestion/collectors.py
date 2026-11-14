"""Native collectors; every result is converted to the same stable domain contract."""

import json
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urlsplit

import feedparser
import yaml

from paoding.core.errors import ConfigurationError, SourceError, TransientError
from paoding.domain.schemas import CollectorItem, CollectorPage
from paoding.ingestion.urls import identify_url
from paoding.integrations.http import ServiceHTTP


def dictionaries(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from dictionaries(child)
    elif isinstance(value, list):
        for child in value:
            yield from dictionaries(child)


def number(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0


def interaction_metrics(data):
    metrics = {}
    for name, aliases in {
        "likes": ("like_count", "digg_count", "liked_count", "likedCount"),
        "comments": ("comment_count", "comments_count"),
        "shares": ("share_count", "shareCount"),
        "saves": ("collect_count", "collected_count"),
        "views": ("view_count", "play_count"),
    }.items():
        for key in aliases:
            if data.get(key) is not None:
                metrics[name] = number(data[key])
                break
    if metrics.get("views", 0) > 0:
        metrics["engagement_rate"] = (
            sum(metrics.get(k, 0) for k in ("likes", "comments", "shares", "saves"))
            / metrics["views"]
        )
    return metrics


def iso_time(value):
    if isinstance(value, (int, float)):
        if value > 1e12:
            value /= 1000
        return datetime.fromtimestamp(value, UTC).isoformat()
    if isinstance(value, str) and value:
        return value
    return None


def snapshot_page(items, cursor, cold_start, scan_complete=True):
    """An exact ID ledger avoids losing same-timestamp or pinned posts. Never silently truncates it."""
    old = json.loads(cursor) if cursor else {"version": 1, "seen": []}
    seen = set(old.get("seen", []))
    unique = {str(item.url): item for item in items}
    if cold_start:
        if not scan_complete:
            # A bounded partial scan cannot truthfully claim the creator's historical Top 20.
            raise ConfigurationError(
                "Creator history exceeds collector_max_pages; increase the cap to compute historical Top 20"
            )
        selected = sorted(
            unique.values(), key=lambda item: item.metrics.get("likes", 0), reverse=True
        )[:20]
        seen.update(unique)
    else:
        candidates = sorted(
            (item for url, item in unique.items() if url not in seen),
            key=lambda item: item.published_at or "",
        )
        selected = candidates[:100]
        seen.update(str(item.url) for item in selected)
        if not scan_complete:
            # There can be undiscovered older posts between the pagination cap and our old cursor.
            raise ConfigurationError(
                "Incremental scan reached its page cap before reaching known content; increase collector_max_pages"
            )
    return CollectorPage(
        items=selected,
        next_cursor=json.dumps({"version": 1, "seen": sorted(seen)}, separators=(",", ":")),
        has_more=not cold_start and len(candidates) > 100,
        scan_complete=scan_complete,
        scanned_count=len(unique),
    )


class TikHubCollector:
    def __init__(self, settings, http=None):
        self.settings, self.http = settings, http or ServiceHTTP()

    def call(self, path, params, method="GET"):
        token = self.settings.tikhub_token.get_secret_value()
        if not token:
            raise ConfigurationError(
                "Configure PAODING_TIKHUB_TOKEN for Douyin/Xiaohongshu, or a collector bridge"
            )
        kwargs = {"params" if method == "GET" else "json": params}
        result = self.http.json(
            method,
            self.settings.tikhub_base_url.rstrip("/") + path,
            headers={"Authorization": f"Bearer {token}"},
            **kwargs,
        )
        if result.get("code") not in {None, 0, 200}:
            raise SourceError("TikHub returned an unsuccessful API status")
        return result.get("data", result)

    def douyin_item(self, obj, url=None):
        video = obj.get("video", {})
        images = obj.get("images", [])
        media = []
        if images:
            for image in images:
                urls = image.get("url_list", [])
                if urls:
                    media.append(urls[-1])
        else:
            for address in [
                video.get("play_addr", {}),
                video.get("play_addr_h264", {}),
                video.get("download_addr", {}),
            ]:
                if address.get("url_list"):
                    media = [address["url_list"][0]]
                    break
        id_ = str(obj.get("aweme_id", ""))
        return CollectorItem(
            url=url or f"https://www.douyin.com/video/{id_}",
            title=obj.get("desc", "")[:300],
            text=obj.get("desc", ""),
            modality="image" if images else "video",
            media_urls=media[:8],
            metrics=interaction_metrics(obj.get("statistics", {})),
            published_at=iso_time(obj.get("create_time")),
        )

    def xhs_item(self, obj, url=None):
        id_ = str(obj.get("note_id", obj.get("id", "")))
        media = []
        kind = "image"
        video = obj.get("video", {})
        for row in dictionaries(video):
            candidate = row.get("master_url") or row.get("url")
            if isinstance(candidate, str) and candidate.startswith("http"):
                media, kind = [candidate], "video"
                break
        if not media:
            for image in obj.get("image_list", obj.get("images", [])):
                url_ = image.get("url_default") or image.get("url_size_large") or image.get("url")
                if not url_:
                    urls = image.get("info_list", [])
                    url_ = urls[-1].get("url") if urls else None
                if url_:
                    media.append(url_)
        return CollectorItem(
            url=url or f"https://www.xiaohongshu.com/explore/{id_}",
            title=obj.get("title", obj.get("display_title", ""))[:300],
            text=obj.get("desc", ""),
            modality=kind,
            media_urls=media[:8],
            metrics=interaction_metrics(obj.get("interact_info", obj)),
            published_at=iso_time(obj.get("time", obj.get("create_time"))),
        )

    def resolve(self, url):
        platform = identify_url(url).platform
        if platform == "douyin":
            data = self.call(
                "/api/v1/douyin/app/v3/fetch_one_video_by_share_url", {"share_url": url}
            )
            row = next(
                (
                    d
                    for d in dictionaries(data)
                    if d.get("aweme_id") and ("video" in d or "images" in d)
                ),
                None,
            )
            if not row:
                raise SourceError("TikHub response has no Douyin content")
            return self.douyin_item(row, url)
        if platform == "xiaohongshu":
            data = self.call(
                "/api/v1/xiaohongshu/app_v2/get_image_note_detail", {"share_text": url}
            )
            row = next(
                (
                    d
                    for d in dictionaries(data)
                    if "image_list" in d or "images" in d or "video" in d
                ),
                None,
            )
            if row and row.get("type") == "video":
                data = self.call(
                    "/api/v1/xiaohongshu/app_v2/get_video_note_detail", {"share_text": url}
                )
                row = next((d for d in dictionaries(data) if "video" in d), row)
            if not row:
                raise SourceError("TikHub response has no Xiaohongshu note")
            return self.xhs_item(row, url)
        raise SourceError("TikHub adapter does not support this platform")

    def account_items(self, url, cursor, cold_start):
        source = identify_url(url)
        user = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
        remote_cursor, items, complete = None, [], False
        for _ in range(self.settings.collector_max_pages):
            if source.platform == "douyin":
                data = self.call(
                    "/api/v1/douyin/app/v3/fetch_user_post_videos",
                    {"sec_user_id": user, "max_cursor": remote_cursor or 0, "count": 20},
                )
                rows = next((d["aweme_list"] for d in dictionaries(data) if "aweme_list" in d), [])
                batch = [self.douyin_item(row) for row in rows]
                remote = next((d for d in dictionaries(data) if "has_more" in d), data)
                next_cursor = remote.get("max_cursor")
            else:
                data = self.call(
                    "/api/v1/xiaohongshu/app_v2/get_user_posted_notes",
                    {"user_id": user, "cursor": remote_cursor or ""},
                )
                rowset = next(
                    (d for d in dictionaries(data) if isinstance(d.get("notes"), list)), {}
                )
                rows = rowset.get("notes", [])
                batch = [self.xhs_item(row.get("note_card", row)) for row in rows]
                remote = next((d for d in dictionaries(data) if "has_more" in d), rowset)
                next_cursor = remote.get("cursor")
            items.extend(batch)
            # Exhaust all returned pages rather than stopping at a pinned known post.
            if not remote.get("has_more"):
                complete = True
                break
            if not next_cursor or next_cursor == remote_cursor:
                raise SourceError("TikHub pagination did not advance")
            remote_cursor = next_cursor
        # Exact full scan; page cap produces an explicit error rather than a false Top 20.
        return snapshot_page(items, cursor, cold_start, complete)


class YtDlpCollector:
    def __init__(self, settings):
        self.settings = settings

    def run(self, args, url):
        if identify_url(url).platform not in {"youtube", "x"}:
            raise SourceError("yt-dlp input must be a supported platform URL")
        command = [
            sys.executable,
            "-m",
            "yt_dlp",
            "--ignore-config",
            "--no-warnings",
            "--no-progress",
            "--socket-timeout",
            "20",
            "--retries",
            "2",
        ]
        command += [
            "--use-extractors",
            "youtube.*" if identify_url(url).platform == "youtube" else "twitter.*",
        ]
        if self.settings.cookies_file:
            command += ["--cookies", str(self.settings.cookies_file.resolve())]
        try:
            result = subprocess.run(
                command + args + ["--", url],
                capture_output=True,
                timeout=self.settings.collector_timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise TransientError("yt-dlp timed out") from exc
        if result.returncode:
            raise TransientError(
                "yt-dlp collection failed; check platform access, cookies, ffmpeg and current yt-dlp version"
            )
        return result.stdout.decode("utf-8")

    def item(self, info):
        return CollectorItem(
            url=info.get("webpage_url") or f"https://www.youtube.com/watch?v={info['id']}",
            title=info.get("title", "")[:300],
            text=info.get("description", ""),
            modality="video",
            metrics=interaction_metrics(info),
            published_at=iso_time(info.get("timestamp")),
        )

    def resolve(self, url):
        root = self.settings.data_dir.resolve() / "media" / "downloads"
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as tmp:
            output = str(Path(tmp) / "%(id)s.%(ext)s")
            raw = self.run(
                [
                    "--no-playlist",
                    "--restrict-filenames",
                    "--max-filesize",
                    str(self.settings.max_download_bytes),
                    "-f",
                    "bv*[height<=720][ext=mp4]+ba[ext=m4a]/b[height<=720][ext=mp4]/b[height<=720]",
                    "--merge-output-format",
                    "mp4",
                    "--write-info-json",
                    "--write-subs",
                    "--write-auto-subs",
                    "--sub-langs",
                    "zh.*,en.*",
                    "--sub-format",
                    "vtt",
                    "-o",
                    output,
                    "--print-json",
                ],
                url,
            )
            info = json.loads(raw.splitlines()[-1])
            item = self.item(info)
            videos = [p for p in Path(tmp).iterdir() if p.suffix in {".mp4", ".webm", ".mkv"}]
            if not videos:
                raise SourceError("yt-dlp returned no downloadable video")
            if any(p.stat().st_size > self.settings.max_download_bytes for p in videos):
                raise SourceError("Downloaded video exceeds configured file limit")
            from paoding.ingestion.normalizer import Normalizer

            normalizer = Normalizer(self.settings)
            item.local_media_paths = [
                normalizer._store(p.read_bytes(), p.suffix) for p in videos[:1]
            ]
            transcripts = []
            for subtitle in sorted(Path(tmp).glob("*.vtt"))[:2]:
                lines = subtitle.read_text(encoding="utf-8").splitlines()
                transcripts.extend(
                    line
                    for line in lines
                    if line
                    and "-->" not in line
                    and not line.startswith(("WEBVTT", "Kind:", "Language:"))
                )
            if transcripts:
                item.text += "\n\n字幕（可能为自动识别）：\n" + "\n".join(
                    dict.fromkeys(transcripts)
                )
            return item

    def account_items(self, url, cursor, cold_start):
        cap = self.settings.collector_max_pages * 20
        raw = self.run(
            ["--skip-download", "--dump-single-json", "--playlist-end", str(cap + 1)], url
        )
        info = json.loads(raw)
        rows = list(info.get("entries") or [])
        valid = [
            entry
            for entry in rows
            if entry and entry.get("id") and entry.get("_type") != "playlist"
        ]
        items = [self.item(entry) for entry in valid[:cap]]
        return snapshot_page(items, cursor, cold_start, len(rows) <= cap)


class GalleryCollector:
    def __init__(self, settings):
        self.settings = settings

    def collect(self, url, limit):
        if identify_url(url).platform != "pinterest":
            raise SourceError("gallery-dl input must be a Pinterest URL")
        command = [
            sys.executable,
            "-m",
            "gallery_dl",
            "--config-ignore",
            "--dump-json",
            "--range",
            f"1-{limit}",
        ]
        if self.settings.cookies_file:
            command += ["--cookies", str(self.settings.cookies_file.resolve())]
        try:
            result = subprocess.run(
                command + ["--", url], capture_output=True, timeout=self.settings.collector_timeout
            )
        except subprocess.TimeoutExpired as exc:
            raise TransientError("gallery-dl timed out") from exc
        if result.returncode:
            raise TransientError("gallery-dl failed; check Pinterest cookies and extractor version")
        messages = json.loads(result.stdout)
        items = {}
        for message in messages:
            if len(message) < 3 or message[0] != 3:
                continue
            media_url, metadata = message[1], message[2]
            pin = str(metadata.get("id", metadata.get("pin_id", "")))
            if not pin:
                continue
            if pin not in items:
                items[pin] = CollectorItem(
                    url=f"https://www.pinterest.com/pin/{pin}/",
                    title=str(metadata.get("title", ""))[:300],
                    text=metadata.get("description", ""),
                    modality="image",
                    metrics=interaction_metrics(metadata),
                    published_at=iso_time(metadata.get("date")),
                )
            if len(items[pin].media_urls) < 8:
                items[pin].media_urls.append(media_url)
        return list(items.values()), len([m for m in messages if len(m) >= 3 and m[0] == 3])

    def resolve(self, url):
        items, _ = self.collect(url, 8)
        if not items:
            raise SourceError("Pinterest extraction returned no images")
        return items[0]

    def account_items(self, url, cursor, cold_start):
        cap = self.settings.collector_max_pages * 20
        items, count = self.collect(url, cap + 1)
        page = snapshot_page(items[:cap], cursor, cold_start, count <= cap)
        if not any("likes" in item.metrics for item in items):
            page.limitations.append(
                "Pinterest extractor did not expose like counts; ranking uses available metrics only"
            )
        return page


class FeedCollector:
    def __init__(self, settings, http=None):
        self.settings, self.http = settings, http or ServiceHTTP()

    def mappings(self):
        if not self.settings.feeds_file.exists():
            return {}
        data = yaml.safe_load(self.settings.feeds_file.read_text(encoding="utf-8")) or {}
        return data.get("feeds", {})

    def feed_url(self, profile):
        mapping = self.mappings().get(profile)
        if mapping:
            url = mapping["feed_url"] if isinstance(mapping, dict) else mapping
            if url.startswith("/"):
                if not self.settings.wewe_rss_url:
                    raise ConfigurationError("Relative feed URLs require PAODING_WEWE_RSS_URL")
                return self.settings.wewe_rss_url.rstrip("/") + url
            return url
        source = identify_url(profile)
        if source.platform == "x" and self.settings.rsshub_url:
            username = urlsplit(profile).path.strip("/").split("/")[0]
            return (
                self.settings.rsshub_url.rstrip("/") + "/twitter/user/" + quote(username, safe="")
            )
        raise ConfigurationError("Add this creator's RSS/WeWe-RSS URL to config/feeds.yaml")

    def entries(self, profile):
        feed_url = self.feed_url(
            profile
        )  # Local RSS endpoints are allowed only via admin configuration.
        data = self.http.request("GET", feed_url).content
        feed = feedparser.parse(data)
        items = []
        for entry in feed.entries:
            url = entry.get("link", "")
            if not url.startswith(("http://", "https://")):
                continue
            content = entry.get("content", [{}])[0].get("value") or entry.get("summary", "")
            from markdownify import markdownify

            published = entry.get("published_parsed") or entry.get("updated_parsed")
            import calendar

            items.append(
                CollectorItem(
                    url=url,
                    title=entry.get("title", "")[:300],
                    text=markdownify(content),
                    published_at=iso_time(calendar.timegm(published)) if published else None,
                    resolved=True,
                )
            )
        return items

    def account_items(self, url, cursor, cold_start):
        items = self.entries(url)
        page = snapshot_page(items, cursor, cold_start)
        page.limitations.append(
            "RSS ranking covers only retained feed entries; historical engagement may be unavailable"
        )
        return page


class NativeCollectors:
    def __init__(self, settings, bridge=None):
        self.settings, self.bridge = settings, bridge
        self.tikhub = TikHubCollector(settings)
        self.youtube = YtDlpCollector(settings)
        self.pinterest = GalleryCollector(settings)
        self.feeds = FeedCollector(settings)

    def resolve(self, url):
        if self.settings.collector_url and self.bridge:
            return self.bridge.resolve(url)
        platform = identify_url(url).platform
        if platform in {"douyin", "xiaohongshu"}:
            return self.tikhub.resolve(url)
        if platform in {"youtube", "x"}:
            if platform == "x" and self.settings.rsshub_url:
                username = urlsplit(url).path.strip("/").split("/")[0]
                for item in self.feeds.entries(f"https://x.com/{username}"):
                    if str(item.url).rstrip("/") == url.rstrip("/"):
                        return item
                raise SourceError("Tweet no longer present in RSSHub feed")
            return self.youtube.resolve(url)
        if platform == "pinterest":
            return self.pinterest.resolve(url)
        if platform == "wechat":
            from paoding.ingestion.http import SafeFetcher

            data, _, _ = SafeFetcher(self.settings.max_download_bytes).fetch(url)
            from bs4 import BeautifulSoup
            from markdownify import markdownify

            soup = BeautifulSoup(data, "html.parser")
            article = soup.select_one("#js_content")
            if article is None:
                raise ConfigurationError(
                    "WeChat article requires a WeWe-RSS feed or WorkBuddy export"
                )
            return CollectorItem(
                url=url,
                title=soup.title.get_text(strip=True) if soup.title else url,
                text=markdownify(str(article)),
            )
        raise ConfigurationError(
            "Use WorkBuddy export or a configured collector for this protected source"
        )

    def account_items(self, url, cursor, cold_start):
        if self.settings.collector_url and self.bridge:
            return self.bridge.account_items(url, cursor, cold_start)
        if url in self.feeds.mappings() or identify_url(url).platform in {"wechat", "x"}:
            return self.feeds.account_items(url, cursor, cold_start)
        platform = identify_url(url).platform
        if platform in {"douyin", "xiaohongshu"}:
            return self.tikhub.account_items(url, cursor, cold_start)
        if platform == "youtube":
            return self.youtube.account_items(url, cursor, cold_start)
        if platform == "pinterest":
            return self.pinterest.account_items(url, cursor, cold_start)
        return self.feeds.account_items(url, cursor, cold_start)
