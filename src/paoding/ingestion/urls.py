import re
from dataclasses import dataclass
from urllib.parse import unquote_plus, urlsplit, urlunsplit

from paoding.core.errors import SourceError

TRACKING = {"fbclid", "gclid", "spm", "share_source", "share_app_id"}


@dataclass(frozen=True)
class Source:
    url: str
    platform: str
    kind: str


def identify_url(value: str) -> Source:
    try:
        parts = urlsplit(value.strip())
        host = (parts.hostname or "").lower().rstrip(".")
        port = parts.port
    except ValueError as exc:
        raise SourceError("Malformed URL") from exc
    if parts.scheme not in {"http", "https"} or not host or parts.username or parts.password:
        raise SourceError("Only HTTP(S) URLs without credentials are accepted")
    if port not in {None, 80, 443}:
        raise SourceError("Source URLs must use standard web ports")
    path = parts.path or "/"
    platform, kind = "web", "item"

    def domain(name):
        return host == name or host.endswith("." + name)

    if domain("xiaohongshu.com") or domain("xhslink.com"):
        platform = "xiaohongshu"
        kind = "creator" if re.match(r"^/user/profile/[^/]+/?$", path) else "item"
    elif domain("douyin.com") or domain("iesdouyin.com"):
        platform = "douyin"
        kind = "creator" if re.match(r"^/user/[^/]+/?$", path) else "item"
    elif host == "mp.weixin.qq.com":
        platform = "wechat"
    elif domain("channels.weixin.qq.com"):
        platform = "wechat_video"
    elif domain("youtube.com") or domain("youtu.be"):
        platform = "youtube"
        kind = (
            "creator"
            if re.match(
                r"^/(?:@[^/]+|channel/[^/]+|c/[^/]+|user/[^/]+)(?:/(?:videos|shorts))?/?$", path
            )
            else "item"
        )
    elif domain("x.com") or domain("twitter.com"):
        platform = "x"
        kind = (
            "creator"
            if re.match(r"^/[^/]+/?$", path)
            and path.strip("/") not in {"home", "search", "explore", "i"}
            else "item"
        )
    elif domain("pinterest.com") or domain("pin.it"):
        platform = "pinterest"
        kind = (
            "creator"
            if domain("pinterest.com")
            and re.match(r"^/[^/]+/?$", path)
            and path.strip("/") != "pin"
            else "item"
        )
    elif domain("digitaling.com"):
        platform = "digitaling"
    elif path.lower().endswith(".pdf"):
        platform = "pdf"
    # Keep signed source parameters; only remove known tracking keys.
    # Preserve raw encoding/order of retained parameters: re-encoding %20 as +
    # can invalidate a CDN signature even when the decoded value is identical.
    query = "&".join(
        piece
        for piece in parts.query.split("&")
        if not unquote_plus(piece.partition("=")[0]).lower().startswith("utm_")
        and unquote_plus(piece.partition("=")[0]).lower() not in TRACKING
    )
    netloc = f"[{host}]" if ":" in host else host
    if port and not (
        (parts.scheme == "https" and port == 443) or (parts.scheme == "http" and port == 80)
    ):
        netloc += f":{port}"
    return Source(urlunsplit((parts.scheme.lower(), netloc, path, query, "")), platform, kind)
