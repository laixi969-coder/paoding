import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image
from pydantic import SecretStr

from paoding.core.errors import ConfigurationError, SourceError
from paoding.domain.schemas import CollectorItem
from paoding.ingestion.collectors import (
    FeedCollector,
    GalleryCollector,
    TikHubCollector,
    YtDlpCollector,
    snapshot_page,
)
from paoding.integrations.http import ServiceHTTP
from paoding.services.worker import Worker
from paoding.storage.tables import Job, KnowledgeNode


def test_snapshot_cold_top20_and_incremental_same_timestamp():
    items = [
        CollectorItem(
            url=f"https://example.com/{i}", metrics={"likes": i}, published_at="2026-01-01"
        )
        for i in range(30)
    ]
    cold = snapshot_page(items, None, True)
    assert len(cold.items) == 20 and cold.items[0].metrics["likes"] == 29
    new = items + [
        CollectorItem(url=f"https://example.com/new-{i}", published_at="2026-01-01")
        for i in range(120)
    ]
    one = snapshot_page(new, cold.next_cursor, False)
    two = snapshot_page(new, one.next_cursor, False)
    assert one.has_more and len(one.items) == 100 and len(two.items) == 20
    assert not two.has_more
    assert snapshot_page(new, two.next_cursor, False).items == []
    with pytest.raises(ConfigurationError):
        snapshot_page(items, None, True, False)


def test_tikhub_douyin_and_xhs_wire_mapping(rt):
    rt.settings.tikhub_token = SecretStr("test-token")
    requests = []
    aweme = {
        "aweme_id": "123",
        "desc": "视频标题",
        "create_time": 1700000000,
        "video": {"play_addr": {"url_list": ["https://cdn.example/video.mp4"]}},
        "statistics": {"digg_count": 20, "comment_count": 5, "play_count": 100},
    }
    note = {
        "note_id": "abc",
        "title": "图文",
        "desc": "正文",
        "image_list": [{"url_default": "https://cdn.example/a.jpg"}],
        "interact_info": {"liked_count": "100", "collected_count": "20"},
    }

    def handle(request):
        requests.append(request)
        if "fetch_user_post" in request.url.path:
            return httpx.Response(
                200, json={"code": 200, "data": {"aweme_list": [aweme], "has_more": False}}
            )
        return httpx.Response(
            200,
            json={
                "code": 200,
                "data": {"aweme_detail": aweme}
                if "douyin" in request.url.path
                else {"data": [{"note_card": note}]},
            },
        )

    collector = TikHubCollector(rt.settings, ServiceHTTP(transport=httpx.MockTransport(handle)))
    item = collector.resolve("https://v.douyin.com/short/")
    assert item.modality == "video" and item.metrics["engagement_rate"] == 0.25
    image = collector.resolve("https://www.xiaohongshu.com/explore/abc")
    assert image.modality == "image" and image.metrics["likes"] == 100
    assert "engagement_rate" not in image.metrics
    page = collector.account_items("https://www.douyin.com/user/SEC_ID", None, True)
    assert page.scanned_count == 1 and page.items[0].title == "视频标题"
    assert requests[0].url.params["share_url"] == "https://v.douyin.com/short/"
    assert requests[-1].url.params["sec_user_id"] == "SEC_ID"


def test_tikhub_incomplete_history_does_not_claim_top20(rt):
    rt.settings.tikhub_token = SecretStr("key")
    rt.settings.collector_max_pages = 1
    client = ServiceHTTP(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={"code": 200, "data": {"aweme_list": [], "has_more": True, "max_cursor": 1}},
            )
        )
    )
    with pytest.raises(ConfigurationError, match="historical Top 20"):
        TikHubCollector(rt.settings, client).account_items(
            "https://douyin.com/user/123", None, True
        )


def test_yt_dlp_download_is_argument_safe_and_archived(rt, monkeypatch):
    seen = []

    def run(args, **kwargs):
        seen.append(args)
        template = args[args.index("-o") + 1]
        Path(template.replace("%(id)s", "ID").replace("%(ext)s", "mp4")).write_bytes(b"video")
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {"id": "ID", "title": "title", "webpage_url": "https://youtube.com/watch?v=ID"}
            ).encode(),
        )

    monkeypatch.setattr("paoding.ingestion.collectors.subprocess.run", run)
    item = YtDlpCollector(rt.settings).resolve("https://youtube.com/watch?v=ID")
    assert item.local_media_paths[0].read_bytes() == b"video"
    assert seen[0][-2] == "--"
    assert "--ignore-config" in seen[0] and "--merge-output-format" in seen[0]
    with pytest.raises(SourceError):
        YtDlpCollector(rt.settings).resolve("https://localhost/secrets")


def test_gallery_cli_option_and_pin_metadata(rt, monkeypatch):
    commands = []

    def run(args, **kw):
        commands.append(args)
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                [[3, "https://cdn.example/image.jpg", {"id": "456", "title": "Pin"}]]
            ).encode(),
        )

    monkeypatch.setattr("paoding.ingestion.collectors.subprocess.run", run)
    item = GalleryCollector(rt.settings).resolve("https://pinterest.com/pin/456")
    assert item.title == "Pin" and item.modality == "image"
    assert "--config-ignore" in commands[0]


def test_feed_snapshot_survives_source_wall(rt, monkeypatch, tmp_path):
    feed = b"""<rss version="2.0"><channel><title>Creator</title><item><title>Article</title><link>https://mp.weixin.qq.com/s/example</link><description>Complete article text</description><pubDate>Mon, 10 Nov 2025 10:00:00 GMT</pubDate></item></channel></rss>"""
    rt.settings.feeds_file = tmp_path / "feeds.yaml"
    rt.settings.feeds_file.write_text(
        'feeds:\n  "https://example.com/creator": "http://127.0.0.1:4000/feed.xml"\n'
    )
    collector = FeedCollector(
        rt.settings,
        ServiceHTTP(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=feed))),
    )
    page = collector.account_items("https://example.com/creator", None, True)
    item = page.items[0]
    assert item.resolved and "Complete article" in item.text
    monkeypatch.setattr(
        rt.normalizer.collector,
        "resolve",
        lambda _: (_ for _ in ()).throw(AssertionError("Must use saved feed body")),
    )
    normalized = rt.normalizer.normalize(
        {"url": str(item.url), "collected_item": item.model_dump(mode="json")}
    )
    assert normalized.text == item.text and normalized.published_at


def test_workbuddy_bundle_and_traversal_rejection(rt, tmp_path):
    directory = tmp_path / "exports"
    directory.mkdir()
    image = directory / "image.png"
    Image.new("RGB", (20, 20), "green").save(image)
    manifest = directory / "case.paoding.json"
    manifest.write_text(
        json.dumps(
            {
                "url": "https://www.xiaohongshu.com/explore/abc",
                "title": "导出案例",
                "text": "原文内容",
                "files": ["image.png"],
            }
        )
    )
    result = rt.ingest_file(manifest)
    worker = Worker(rt)
    while worker.run_once():
        pass
    with rt.db.transaction() as session:
        job = session.get(Job, result["job_id"])
        node = session.get(KnowledgeNode, job.result["node_id"])
        assert node.platform == "xiaohongshu" and "导出案例" in node.title
    manifest.write_text(json.dumps({"files": ["../secret.txt"]}))
    with pytest.raises(SourceError, match="manifest directory"):
        rt.ingest_file(manifest)
