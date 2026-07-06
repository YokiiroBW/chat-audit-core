from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "chat-audit-core"
    app_env: str = "development"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    app_secret_key: str = "change-me"
    system_instance_id: str = "chat-audit-core"
    admin_api_token: str = ""
    admin_api_tokens: str = ""

    database_url: str = "sqlite+aiosqlite:///./data/chat_audit.sqlite3"
    database_pool_size: int = 20
    database_max_overflow: int = 10
    database_pool_timeout_seconds: int = 30
    database_pool_recycle_seconds: int = 3600
    api_max_request_body_bytes: int = 104857600

    storage_root: Path = Path("./data/storage")
    backup_root: Path = Path("./data/backups")
    public_storage_prefix: str = "/static/storage"

    onebot_ws_path: str = "/onebot/v11/ws"
    onebot_access_token: str = ""

    media_download_timeout_seconds: int = 30
    media_max_bytes: int = 104857600
    ffmpeg_bin: str = "ffmpeg"
    ffmpeg_library_path: str = ""
    media_transcode_enabled: bool = False
    media_transcode_voice_ext: str = "mp3"
    media_transcode_video_ext: str = "mp4"
    high_risk_rate_limit_per_minute: int = 10
    csrf_enabled: bool = True
    csrf_secure_cookie: bool = False

    auto_backup_cron: str = "0 3 * * *"
    auto_backup_keep_latest: int = 7

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
