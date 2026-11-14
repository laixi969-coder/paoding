from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PAODING_", env_file=".env", extra="ignore")
    database_url: str = "postgresql+psycopg://paoding:paoding@127.0.0.1:5432/paoding"
    data_dir: Path = Path("data")
    vault_dir: Path = Path("PaoDing_Vault")
    drop_dir: Path = Path("PaoDing_Drop")
    models_file: Path = Path("config/models.yaml")
    api_token: SecretStr = SecretStr("")
    host: str = "127.0.0.1"
    port: int = 8010
    timezone: str = "Asia/Shanghai"
    demo_mode: bool = False
    max_download_bytes: int = 256 * 1024 * 1024
    max_text_chars: int = 60000
    model_timeout: float = 60
    connect_timeout: float = 1
    lease_seconds: int = Field(default=300, ge=15)
    max_attempts: int = Field(default=3, ge=1, le=10)
    collector_url: str = ""
    collector_token: SecretStr = SecretStr("")
    telegram_secret: SecretStr = SecretStr("")
    telegram_bot_token: SecretStr = SecretStr("")
    feishu_verification_token: SecretStr = SecretStr("")
    embedding_provider: str = ""
    embedding_model: str = ""
    tikhub_token: SecretStr = SecretStr("")
    tikhub_base_url: str = "https://api.tikhub.io"
    collector_max_pages: int = Field(default=20, ge=1, le=1000)
    collector_timeout: int = Field(default=600, ge=10)
    cookies_file: Path | None = None
    feeds_file: Path = Path("config/feeds.yaml")
    rsshub_url: str = ""
    wewe_rss_url: str = ""
    webpage_reader: Literal["local", "jina", "firecrawl"] = "local"
    jina_token: SecretStr = SecretStr("")
    firecrawl_token: SecretStr = SecretStr("")
    firecrawl_url: str = "https://api.firecrawl.dev/v1"
    mineru_mode: Literal["auto", "cloud", "local", "text"] = "auto"
    mineru_token: SecretStr = SecretStr("")
    mineru_url: str = "https://mineru.net/api/v4"
    mineru_command: str = "mineru"
    document_timeout: int = Field(default=600, ge=10)
    ffmpeg_command: str = "ffmpeg"
    ffprobe_command: str = "ffprobe"
    video_segment_seconds: int = Field(default=120, ge=10, le=600)
    media_timeout: int = Field(default=600, ge=10)
    gemini_file_timeout: int = Field(default=180, ge=10)
    transcription_provider: str = ""
    transcription_model: str = ""
    feishu_encrypt_key: SecretStr = SecretStr("")
    feishu_app_id: str = ""
    feishu_app_secret: SecretStr = SecretStr("")
    wecom_corp_id: str = ""
    wecom_agent_id: int = 0
    wecom_corp_secret: SecretStr = SecretStr("")
    wecom_token: SecretStr = SecretStr("")
    wecom_encoding_aes_key: SecretStr = SecretStr("")
    webhook_max_age_seconds: int = Field(default=600, ge=60)
    taxonomy_min_sources: int = Field(default=20, ge=2)
    workbuddy_dir: Path | None = None
    text_route: str = ""
    image_route: str = ""
    video_route: str = ""
    deepseek_base_url: str = ""
    qwen_base_url: str = ""
    gemini_base_url: str = ""
    doubao_base_url: str = ""
    anthropic_base_url: str = ""
    openai_base_url: str = ""
    google_oauth_client_id: str = ""
    google_oauth_client_secret: SecretStr = SecretStr("")
    google_project_id: str = ""

    @field_validator("cookies_file", "workbuddy_dir", mode="before")
    @classmethod
    def empty_optional_path(cls, value):
        return None if isinstance(value, str) and not value.strip() else value

    @model_validator(mode="after")
    def validate_runtime(self):
        ZoneInfo(self.timezone)
        if (
            self.host not in {"127.0.0.1", "localhost", "::1"}
            and not self.api_token.get_secret_value()
        ):
            raise ValueError("Binding beyond localhost requires PAODING_API_TOKEN")
        if self.demo_mode and not self.database_url.startswith("sqlite"):
            raise ValueError("Demo mode requires a separate SQLite database")
        return self

    def prepare_directories(self):
        for path in (self.data_dir, self.vault_dir, self.drop_dir):
            path.mkdir(parents=True, exist_ok=True)
