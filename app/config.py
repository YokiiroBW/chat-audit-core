from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "chat-audit-core"
    app_env: str = "development"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    app_secret_key: str = "change-me"
    log_level: str = "INFO"
    system_instance_id: str = "chat-audit-core"
    admin_api_token: str = ""
    admin_api_tokens: str = ""
    # Product-local, read-only evidence candidate. Empty/invalid denies access;
    # independent hashed service credentials and exact channel registrations.
    evidence_read_config: str = ""

    database_url: str = "sqlite+aiosqlite:///./data/chat_audit.sqlite3"
    database_pool_size: int = 20
    database_max_overflow: int = 10
    database_pool_timeout_seconds: int = 30
    database_pool_recycle_seconds: int = 3600
    api_max_request_body_bytes: int = 104857600

    storage_root: Path = Path("./data/storage")
    backup_root: Path = Path("./data/backups")
    public_storage_prefix: str = "/media"
    auth_session_cookie_name: str = "chat_audit_session"
    auth_session_cookie_secure: bool = False
    # Browser login lifetime. Applies to sessions created before the column
    # existed too, measured from their creation, so no session outlives the
    # policy by predating it. Set to 0 to keep sessions valid until revoked.
    admin_session_ttl_hours: int = 12
    # API tokens are long-lived by design; 0 keeps the previous behaviour of
    # never expiring, and a positive value ages them out from creation.
    admin_token_ttl_days: int = 0
    # Comma-separated proxy addresses or CIDRs whose X-Forwarded-For header may
    # be believed. Empty, the default, means the header is ignored: it is
    # caller-supplied, and trusting it lets anyone forge the audited address and
    # sidestep per-address rate limits.
    trusted_proxy_ips: str = ""

    onebot_ws_path: str = "/onebot/v11/ws"
    onebot_access_token: str = ""
    onebot_heartbeat_interval_seconds: float = 30
    onebot_heartbeat_timeout_seconds: float = 10

    media_download_timeout_seconds: int = 30
    media_max_bytes: int = 104857600
    ffmpeg_bin: str = "ffmpeg"
    ffmpeg_library_path: str = ""
    media_transcode_enabled: bool = False
    media_transcode_timeout_seconds: int = 60
    media_transcode_voice_ext: str = "mp3"
    media_transcode_video_ext: str = "mp4"
    forward_cache_max_depth: int = 3
    high_risk_rate_limit_per_minute: int = 10
    csrf_enabled: bool = True
    csrf_secure_cookie: bool = False

    auto_backup_cron: str = "0 3 * * *"
    auto_backup_keep_latest: int = 7
    backup_chunk_bytes: int = 8388608
    backup_min_free_bytes: int = 536870912
    backup_worker_poll_seconds: float = 5.0
    # A second boundary below the worker container's 768 MiB cgroup limit. The
    # archive implementation targets <=512 MiB RSS; RLIMIT_AS turns an
    # accidental regression into a failed job before it can pressure dockerd.
    backup_worker_address_space_limit_bytes: int = 671088640
    backup_worker_heartbeat_max_age_seconds: int = 120
    # v3 JSON remains only as a bounded compatibility export. Full backups use
    # the worker's v4 archive and can never reach this in-process path.
    backup_legacy_export_max_messages: int = 10000
    backup_legacy_export_max_total_media_bytes: int = 67108864
    backup_legacy_import_max_bytes: int = 134217728
    # Reject import packages that carry no checksum or no verifiable signature.
    # Off by default so unsigned legacy packages stay importable; turn it on to
    # require that every imported archive proves where it came from.
    backup_import_require_signature: bool = False

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
