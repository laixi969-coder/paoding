"""Allowlisted local settings. Existing secrets are never returned by the API."""

import os
import threading

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field

from paoding.core.config import Settings
from paoding.storage.credentials import update_dotenv

LOCK = threading.Lock()

FIELDS = {
    "DEEPSEEK_API_KEY": ("DeepSeek API Key", "模型", True),
    "QWEN_API_KEY": ("通义千问 API Key", "模型", True),
    "GEMINI_API_KEY": ("Gemini API Key", "模型", True),
    "DOUBAO_API_KEY": ("豆包 API Key", "模型", True),
    "ANTHROPIC_API_KEY": ("Claude API Key", "模型", True),
    "OPENAI_API_KEY": ("OpenAI API Key", "模型", True),
    "PAODING_GOOGLE_OAUTH_CLIENT_ID": ("Google OAuth Client ID", "Google 授权", False),
    "PAODING_GOOGLE_OAUTH_CLIENT_SECRET": ("Google OAuth Client Secret", "Google 授权", True),
    "PAODING_GOOGLE_PROJECT_ID": ("Google Cloud 项目 ID", "Google 授权", False),
    "PAODING_DEEPSEEK_BASE_URL": ("DeepSeek Base URL", "模型", False),
    "PAODING_QWEN_BASE_URL": ("通义千问 Base URL", "模型", False),
    "PAODING_GEMINI_BASE_URL": ("Gemini Base URL", "模型", False),
    "PAODING_DOUBAO_BASE_URL": ("豆包 Base URL", "模型", False),
    "PAODING_ANTHROPIC_BASE_URL": ("Claude Base URL", "模型", False),
    "PAODING_OPENAI_BASE_URL": ("OpenAI Base URL", "模型", False),
    "PAODING_TEXT_ROUTE": ("文字模型路由", "模型路由", False),
    "PAODING_IMAGE_ROUTE": ("图片模型路由", "模型路由", False),
    "PAODING_VIDEO_ROUTE": ("视频模型路由", "模型路由", False),
    "PAODING_TIKHUB_TOKEN": ("TikHub API Token", "采集", True),
    "PAODING_RSSHUB_URL": ("RSSHub 地址", "采集", False),
    "PAODING_WEWE_RSS_URL": ("WeWe-RSS 地址", "采集", False),
    "PAODING_COOKIES_FILE": ("浏览器 Cookie 文件路径", "采集", False),
    "PAODING_MINERU_TOKEN": ("MinerU Token", "文档", True),
    "PAODING_MINERU_MODE": ("MinerU 模式（auto / cloud / local / text）", "文档", False),
    "PAODING_WEBPAGE_READER": ("网页读取方式（local / jina / firecrawl）", "文档", False),
    "PAODING_FIRECRAWL_URL": ("Firecrawl 服务地址", "文档", False),
    "PAODING_FIRECRAWL_TOKEN": ("Firecrawl Token", "文档", True),
    "PAODING_JINA_TOKEN": ("Jina Token", "文档", True),
    "PAODING_TELEGRAM_BOT_TOKEN": ("Telegram Bot Token", "Telegram", True),
    "PAODING_TELEGRAM_SECRET": ("Telegram Webhook Secret", "Telegram", True),
    "PAODING_FEISHU_APP_ID": ("飞书 App ID", "飞书", False),
    "PAODING_FEISHU_APP_SECRET": ("飞书 App Secret", "飞书", True),
    "PAODING_FEISHU_VERIFICATION_TOKEN": ("飞书 Verification Token", "飞书", True),
    "PAODING_FEISHU_ENCRYPT_KEY": ("飞书 Encrypt Key", "飞书", True),
    "PAODING_WECOM_CORP_ID": ("企业微信 Corp ID", "企业微信", False),
    "PAODING_WECOM_AGENT_ID": ("企业微信 Agent ID", "企业微信", False),
    "PAODING_WECOM_CORP_SECRET": ("企业微信 Corp Secret", "企业微信", True),
    "PAODING_WECOM_TOKEN": ("企业微信回调 Token", "企业微信", True),
    "PAODING_WECOM_ENCODING_AES_KEY": ("企业微信 EncodingAESKey", "企业微信", True),
    "PAODING_TRANSCRIPTION_PROVIDER": ("语音转写服务商", "高级", False),
    "PAODING_TRANSCRIPTION_MODEL": ("语音转写模型", "高级", False),
    "PAODING_EMBEDDING_PROVIDER": ("向量服务商", "高级", False),
    "PAODING_EMBEDDING_MODEL": ("向量模型", "高级", False),
    "PAODING_COLLECTOR_URL": ("自有采集网关地址", "高级", False),
    "PAODING_COLLECTOR_TOKEN": ("自有采集网关 Token", "高级", True),
}


class SettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    values: dict[str, str] = Field(max_length=len(FIELDS))


def metadata():
    values = dotenv_values(".env")
    import yaml

    path = Settings().models_file
    defaults = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    result = []
    for name, (label, group, secret) in FIELDS.items():
        value = os.environ.get(name) or values.get(name) or ""
        if not value and name.startswith("PAODING_") and name.endswith("_BASE_URL"):
            provider = name.removeprefix("PAODING_").removesuffix("_BASE_URL").lower()
            value = defaults.get("providers", {}).get(provider, {}).get("base_url", "")
        result.append(
            {
                "key": name,
                "label": label,
                "group": group,
                "secret": secret,
                "configured": bool(value and value != "0"),
                "value": "" if secret else value,
                "environment_override": bool(os.environ.get(name)),
            }
        )
    return result


def update_values(values, settings=None):
    for name, value in values.items():
        if name not in FIELDS or len(value) > 4096 or any(ord(c) < 32 for c in value):
            raise ValueError("包含不支持的配置项或字符")
        if name.endswith("_URL") and value and not value.startswith(("http://", "https://")):
            raise ValueError(f"{FIELDS[name][0]}须以 http:// 或 https:// 开头")
        if name.endswith("_BASE_URL") and value:
            from urllib.parse import urlsplit

            parts = urlsplit(value)
            if (
                not parts.hostname
                or parts.username
                or parts.password
                or parts.query
                or parts.fragment
            ):
                raise ValueError("模型 Base URL 不可包含账号密码、查询参数或片段")
            if parts.scheme != "https" and parts.hostname not in {"127.0.0.1", "localhost", "::1"}:
                raise ValueError("非本机模型 Base URL 必须使用 HTTPS")
        if name == "PAODING_WECOM_AGENT_ID" and value and not value.isdigit():
            raise ValueError("企业微信 Agent ID 必须为数字")
        if name == "PAODING_MINERU_MODE" and value not in {"auto", "cloud", "local", "text"}:
            raise ValueError("MinerU 模式须为 auto、cloud、local 或 text")
        if name == "PAODING_WEBPAGE_READER" and value not in {"local", "jina", "firecrawl"}:
            raise ValueError("网页读取方式须为 local、jina 或 firecrawl")
        if name in {"PAODING_TEXT_ROUTE", "PAODING_IMAGE_ROUTE", "PAODING_VIDEO_ROUTE"} and value:
            import re

            from paoding.models.custom import BUILTINS, load_custom

            allowed = BUILTINS | set(load_custom(settings or Settings()))

            for part in value.split(","):
                provider, separator, model = part.strip().partition(":")
                if (
                    provider not in allowed
                    or not separator
                    or not re.fullmatch(r"[A-Za-z0-9._/:@+\-]+", model)
                ):
                    raise ValueError("模型路由格式应为 服务商:模型ID，例如 qwen:qwen-plus")
    with LOCK:
        # Blank secret fields retain the existing credential.
        update_dotenv(
            {name: value for name, value in values.items() if value or not FIELDS[name][2]}
        )


def refresh_settings(current):
    """Refresh allowlisted settings in-place so existing components share new values."""
    latest = Settings()
    for name in FIELDS:
        if name.startswith("PAODING_"):
            attr = name.removeprefix("PAODING_").lower()
            setattr(current, attr, getattr(latest, attr))
