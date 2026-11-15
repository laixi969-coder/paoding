"""Shared, server-rendered workspace navigation for the four mobile pages."""

from pathlib import Path

from fastapi.responses import HTMLResponse

PAGES = (
    ("library", "知识库"),
    ("capture", "收录内容"),
    ("guide", "接入指南"),
    ("settings", "模型设置"),
)


def workspace_page(
    page: str, *, auth_required: bool = False, local_only: bool = False
) -> HTMLResponse:
    links = "".join(
        f'<a href="/{name}"' + (' aria-current="page"' if name == page else "") + f">{label}</a>"
        for name, label in PAGES
    )
    navigation = (
        '<a class="workspace-skip" href="#main-content">跳到正文</a>'
        '<header class="workspace-mobile-header">'
        '<a class="workspace-brand" href="/library" aria-label="庖丁，返回知识库">庖丁</a>'
        f'<nav class="workspace-mobile-nav" aria-label="主导航">{links}</nav></header>'
    )
    template = "settings_local" if local_only else page
    html = Path(__file__).with_name(f"{template}.html").read_text(encoding="utf-8")
    html = html.replace("__PAODING_MOBILE_NAV__", navigation).replace(
        "__PAODING_AUTH_REQUIRED__", "true" if auth_required else "false"
    )
    return HTMLResponse(
        html,
        status_code=403 if local_only else 200,
        headers={
            "Cache-Control": "no-store",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
        },
    )
