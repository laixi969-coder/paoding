"""Local model catalog. Secrets stay in the existing private .env file."""

import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field

from paoding.core.settings_panel import LOCK
from paoding.storage.credentials import update_dotenv
from paoding.storage.vault import atomic_write

BUILTINS = {
    "deepseek",
    "qwen",
    "gemini",
    "doubao",
    "anthropic",
    "openai",
    "chatgpt",
    "gemini_google",
}
PRESETS = {
    "openrouter": ("OpenRouter", "https://openrouter.ai/api/v1"),
    "atlascloud": ("Atlas Cloud", "https://api.atlascloud.ai/v1"),
}


class ProviderInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    name: str = Field(min_length=1, max_length=80)
    base_url: str = Field(min_length=1, max_length=500)
    api_key: str = Field(default="", max_length=4096)
    video_url_support: bool = False
    video_audio: bool = False


class ModelInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    model: str = Field(min_length=1, max_length=200)


def valid_model(model):
    if not re.fullmatch(r"[A-Za-z0-9._/:@+\-]+", model):
        raise ValueError("模型 ID 只能包含字母、数字、斜杠和常见符号")
    return model


def validate_url(url):
    try:
        parts = urlsplit(url)
        _ = parts.port
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
        ):
            raise ValueError("请输入不含账号密码或查询参数的 HTTP(S) Base URL")
        if parts.scheme == "http" and parts.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("非本机中转服务须使用 HTTPS")
    except ValueError as exc:
        raise ValueError("Base URL 无效或不安全") from exc
    return url.rstrip("/")


def registry_path(settings):
    return Path(settings.data_dir) / "custom_providers.json"


def load_custom(settings):
    path = registry_path(settings)
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("自定义服务商配置格式无效")
    return data


def write_custom(settings, data):
    atomic_write(registry_path(settings), json.dumps(data, ensure_ascii=False, indent=2).encode())


def custom_key_env(provider_id):
    return "PAODING_CUSTOM_" + provider_id.upper() + "_API_KEY"


def save_secret(env_name, secret):
    if not secret:
        return
    if len(secret) > 4096 or any(ord(c) < 32 for c in secret):
        raise ValueError("API Key 格式无效")
    update_dotenv({env_name: secret})


def save_custom(settings, item):
    if item.id in BUILTINS:
        raise ValueError("不能覆盖内置服务商")
    url = validate_url(item.base_url)
    if any(ord(c) < 32 for c in item.name):
        raise ValueError("服务商名称格式无效")
    if item.api_key and os.getenv(custom_key_env(item.id)):
        raise ValueError("该服务商的系统环境变量覆盖本地密钥，请先移除环境变量")
    with LOCK:
        registry = load_custom(settings)
        # Store the key first: a filesystem error must not produce a configured provider without its intended key.
        save_secret(custom_key_env(item.id), item.api_key)
        registry[item.id] = {
            "name": item.name,
            "base_url": url,
            "video_url_support": item.video_url_support,
            "video_audio": item.video_audio,
        }
        write_custom(settings, registry)
    return {
        "id": item.id,
        "name": item.name,
        "base_url": url,
        "configured": bool(
            os.getenv(custom_key_env(item.id)) or dotenv_values(".env").get(custom_key_env(item.id))
        ),
        "video_url_support": item.video_url_support,
        "video_audio": item.video_audio,
    }


def custom_public(settings):
    values = dotenv_values(".env")
    return [
        {
            "id": key,
            **item,
            "configured": bool(os.getenv(custom_key_env(key)) or values.get(custom_key_env(key))),
            "environment_override": bool(os.getenv(custom_key_env(key))),
        }
        for key, item in load_custom(settings).items()
    ]
