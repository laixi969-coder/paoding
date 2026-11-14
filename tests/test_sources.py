import socket

import pytest

from paoding.core.errors import SourceError
from paoding.ingestion.http import SafeFetcher
from paoding.ingestion.urls import identify_url


@pytest.mark.parametrize(
    "url,platform,kind",
    [
        ("https://www.xiaohongshu.com/user/profile/abc", "xiaohongshu", "creator"),
        ("https://www.xiaohongshu.com/explore/abc", "xiaohongshu", "item"),
        ("https://www.douyin.com/user/abc", "douyin", "creator"),
        ("https://mp.weixin.qq.com/s/abc", "wechat", "item"),
        ("https://youtube.com/@example/videos", "youtube", "creator"),
        ("https://youtu.be/abc", "youtube", "item"),
        ("https://x.com/person/status/123", "x", "item"),
        ("https://pinterest.com/person/", "pinterest", "creator"),
        ("https://pinterest.com/pin/123", "pinterest", "item"),
        ("https://example.com/doc.pdf", "pdf", "item"),
        ("https://douyin.com.evil.example/user/abc", "web", "item"),
    ],
)
def test_platform_recognition(url, platform, kind):
    result = identify_url(url)
    assert (result.platform, result.kind) == (platform, kind)


def test_tracking_removed_signed_parameters_preserved():
    source = identify_url("https://EXAMPLE.com/a?utm_source=x&xsec_token=secret&x=1#frag")
    assert source.url == "https://example.com/a?xsec_token=secret&x=1"
    assert (
        identify_url("https://example.com/a?signature=a%20b%2fc&x=&x=2").url
        == "https://example.com/a?signature=a%20b%2fc&x=&x=2"
    )


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "https://user:pass@example.com",
        "http://example.com:8080",
        "javascript:foo",
    ],
)
def test_rejects_invalid_sources(url):
    with pytest.raises(SourceError):
        identify_url(url)


@pytest.mark.parametrize(
    "address", ["127.0.0.1", "10.1.2.3", "169.254.169.254", "::1", "::ffff:127.0.0.1"]
)
def test_fetcher_blocks_private_addresses(monkeypatch, address):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 80))],
    )
    with pytest.raises(SourceError, match="addresses"):
        SafeFetcher(1000).fetch("https://example.com")


def test_redirect_is_validated_and_connection_uses_pinned_ip(monkeypatch):
    import httpx

    import paoding.ingestion.http as source_http

    requests = []

    def resolve(host, *args, **kwargs):
        ip = "93.184.216.34" if host == "example.com" else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))]

    def respond(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": "http://internal.example/secret"})

    real_client = httpx.Client
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(
        source_http.httpx,
        "Client",
        lambda **kw: real_client(transport=httpx.MockTransport(respond), **kw),
    )
    with pytest.raises(SourceError):
        SafeFetcher(1000).fetch("https://example.com")
    assert len(requests) == 1
    assert requests[0].url.host == "93.184.216.34"
    assert requests[0].headers["Host"] == "example.com"
    assert requests[0].extensions["sni_hostname"] == "example.com"


def test_download_size_limit_is_enforced(monkeypatch):
    import httpx

    import paoding.ingestion.http as source_http

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
    )
    real_client = httpx.Client
    monkeypatch.setattr(
        source_http.httpx,
        "Client",
        lambda **kw: real_client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"123456")), **kw
        ),
    )
    with pytest.raises(SourceError, match="download limit"):
        SafeFetcher(5).fetch("https://example.com")
