from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "chat-audit-core"
    app_env: str = "development"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    app_secret_key: str = "change-me"

    database_url: str = "sqlite+aiosqlite:///./data/chat_audit.sqlite3"

    storage_root: Path = Path("./data/storage")
    backup_root: Path = Path("./data/backups")
    public_storage_prefix: str = "/static/storage"

    onebot_ws_path: str = "/onebot/v11/ws"
    onebot_access_token: str = ""

    media_download_timeout_seconds: int = 30
    media_max_bytes: int = 104857600
    ffmpeg_bin: str = "ffmpeg"

    auto_backup_cron: str = "0 3 * * *"
    auto_backup_keep_latest: int = 7

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
