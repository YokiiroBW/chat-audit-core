from __future__ import annotations

import os
import socket
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class CollectorConfigError(ValueError):
    pass


SENSITIVE_CONFIG_KEYS = frozenset(
    {
        "api_key",
        "api_token",
        "client_private_key",
        "database_key",
        "db_key",
        "password",
        "qqnt_database_key",
        "secret",
        "token",
    }
)


def default_collector_root() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "ChatAuditQQCollector"
    return Path.home() / ".chat-audit-qq-collector"


@dataclass(frozen=True)
class CollectorPaths:
    root: Path
    state_db: Path
    credential_store: Path
    staging_dir: Path
    log_dir: Path

    @classmethod
    def from_root(cls, root: Path) -> "CollectorPaths":
        resolved = root.expanduser().resolve()
        return cls(
            root=resolved,
            state_db=resolved / "collector_state.sqlite3",
            credential_store=resolved / "credentials.dpapi",
            staging_dir=resolved / "staging",
            log_dir=resolved / "logs",
        )

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class CollectorSettings:
    account_id: str
    device_id: str
    device_name: str
    sync_interval_minutes: int = 10
    sync_interval_seconds: int = 600
    autostart: bool = False
    reconcile_interval_hours: int = 24
    staging_max_gb: int = 20
    initial_import_batch_size: int = 200
    incremental_batch_size: int = 100


@dataclass(frozen=True)
class QQSettings:
    data_root: Path | None = None
    install_dir: Path | None = None
    read_mode: str = "direct_readonly"
    enable_vss_fallback: bool = True


@dataclass(frozen=True)
class ServerSettings:
    base_url: str
    source_id: str | None = None
    request_timeout_seconds: int = 60
    verify_tls: bool = True


@dataclass(frozen=True)
class MediaSettings:
    copy_before_upload: bool = True
    validate_video_with_ffprobe: bool = True
    upload_thumbnails: bool = True
    rescan_not_downloaded_days: int = 30


@dataclass(frozen=True)
class CollectorConfig:
    collector: CollectorSettings
    qq: QQSettings
    server: ServerSettings
    media: MediaSettings
    paths: CollectorPaths
    config_path: Path


def _as_table(document: dict[str, Any], name: str) -> dict[str, Any]:
    value = document.get(name, {})
    if not isinstance(value, dict):
        raise CollectorConfigError(f"[{name}] must be a TOML table")
    return value


def _required_string(table: dict[str, Any], field: str, section: str) -> str:
    value = table.get(field)
    if not isinstance(value, str) or not value.strip():
        raise CollectorConfigError(f"[{section}].{field} must be a non-empty string")
    return value.strip()


def _optional_string(table: dict[str, Any], field: str) -> str | None:
    value = table.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise CollectorConfigError(f"{field} must be a string or omitted")
    return value.strip() or None


def _positive_int(table: dict[str, Any], field: str, default: int) -> int:
    value = table.get(field, default)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise CollectorConfigError(f"{field} must be a positive integer")
    return value


def _bool_value(table: dict[str, Any], field: str, default: bool) -> bool:
    value = table.get(field, default)
    if not isinstance(value, bool):
        raise CollectorConfigError(f"{field} must be a boolean")
    return value


def _reject_sensitive_values(value: Any, path: str = "") -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = str(key).strip().lower()
            field_path = f"{path}.{key}" if path else str(key)
            if normalized in SENSITIVE_CONFIG_KEYS or normalized.endswith("_token") or normalized.endswith("_secret"):
                raise CollectorConfigError(
                    f"sensitive field {field_path!r} is forbidden in TOML; use the DPAPI credential store"
                )
            _reject_sensitive_values(nested, field_path)
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _reject_sensitive_values(nested, f"{path}[{index}]")


def load_config(path: str | Path) -> CollectorConfig:
    config_path = Path(path).expanduser().resolve()
    try:
        document = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise CollectorConfigError(f"unable to read collector config: {exc}") from exc
    if not isinstance(document, dict):
        raise CollectorConfigError("collector config must be a TOML document")
    _reject_sensitive_values(document)

    collector_table = _as_table(document, "collector")
    qq_table = _as_table(document, "qq")
    server_table = _as_table(document, "server")
    media_table = _as_table(document, "media")

    account_id = _required_string(collector_table, "account_id", "collector")
    device_name = _required_string(collector_table, "device_name", "collector")
    device_id = _optional_string(collector_table, "device_id") or socket.gethostname().strip() or "windows-host"
    root_value = _optional_string(collector_table, "data_dir")
    root = Path(os.path.expandvars(root_value)).expanduser() if root_value else default_collector_root()

    data_root_value = _optional_string(qq_table, "data_root")
    data_root = Path(os.path.expandvars(data_root_value)).expanduser() if data_root_value else None
    install_dir_value = _optional_string(qq_table, "install_dir")
    install_dir = Path(os.path.expandvars(install_dir_value)).expanduser() if install_dir_value else None
    read_mode = _optional_string(qq_table, "read_mode") or "direct_readonly"
    if read_mode not in {"direct_readonly", "snapshot_copy"}:
        raise CollectorConfigError("[qq].read_mode must be direct_readonly or snapshot_copy")

    base_url = _required_string(server_table, "base_url", "server").rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        raise CollectorConfigError("[server].base_url must use http:// or https://")

    legacy_interval_minutes = _positive_int(collector_table, "sync_interval_minutes", 10)
    interval_seconds = _positive_int(collector_table, "sync_interval_seconds", legacy_interval_minutes * 60)
    return CollectorConfig(
        collector=CollectorSettings(
            account_id=account_id,
            device_id=device_id,
            device_name=device_name,
            sync_interval_minutes=legacy_interval_minutes,
            sync_interval_seconds=interval_seconds,
            autostart=_bool_value(collector_table, "autostart", False),
            reconcile_interval_hours=_positive_int(collector_table, "reconcile_interval_hours", 24),
            staging_max_gb=_positive_int(collector_table, "staging_max_gb", 20),
            initial_import_batch_size=_positive_int(collector_table, "initial_import_batch_size", 200),
            incremental_batch_size=_positive_int(collector_table, "incremental_batch_size", 100),
        ),
        qq=QQSettings(
            data_root=data_root,
            install_dir=install_dir,
            read_mode=read_mode,
            enable_vss_fallback=_bool_value(qq_table, "enable_vss_fallback", True),
        ),
        server=ServerSettings(
            base_url=base_url,
            source_id=_optional_string(server_table, "source_id"),
            request_timeout_seconds=_positive_int(server_table, "request_timeout_seconds", 60),
            verify_tls=_bool_value(server_table, "verify_tls", True),
        ),
        media=MediaSettings(
            copy_before_upload=_bool_value(media_table, "copy_before_upload", True),
            validate_video_with_ffprobe=_bool_value(media_table, "validate_video_with_ffprobe", True),
            upload_thumbnails=_bool_value(media_table, "upload_thumbnails", True),
            rescan_not_downloaded_days=_positive_int(media_table, "rescan_not_downloaded_days", 30),
        ),
        paths=CollectorPaths.from_root(root),
        config_path=config_path,
    )
