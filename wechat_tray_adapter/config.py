from __future__ import annotations

from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
from typing import Any, Mapping


APP_DIR_NAME = "ChatAuditWechatTray"


def default_app_dir(env: Mapping[str, str] | None = None) -> Path:
    values = env or os.environ
    base = values.get("APPDATA") or values.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / APP_DIR_NAME


@dataclass(frozen=True)
class AdapterConfig:
    nas_url: str = "http://127.0.0.1:8000"
    token: str = ""
    account_id: str = ""
    account_name: str | None = None
    auto_download_media: bool = True
    autostart: bool = False
    paused: bool = False
    queue_db: Path | None = None
    log_dir: Path | None = None
    retry_interval_seconds: int = 10

    @classmethod
    def default(cls, env: Mapping[str, str] | None = None) -> "AdapterConfig":
        app_dir = default_app_dir(env)
        return cls(queue_db=app_dir / "queue.sqlite3", log_dir=app_dir / "logs")

    @classmethod
    def load(cls, path: str | Path | None = None, env: Mapping[str, str] | None = None) -> "AdapterConfig":
        values = env or os.environ
        config = cls.default(values)
        config_path = Path(path) if path is not None else default_app_dir(values) / "config.json"
        if config_path.exists():
            raw = json.loads(config_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("config.json must contain an object")
            config = config.with_overrides(raw)

        env_overrides: dict[str, Any] = {}
        if values.get("CHAT_AUDIT_NAS_URL"):
            env_overrides["nas_url"] = values["CHAT_AUDIT_NAS_URL"]
        if values.get("CHAT_AUDIT_TOKEN"):
            env_overrides["token"] = values["CHAT_AUDIT_TOKEN"]
        if values.get("CHAT_AUDIT_WECHAT_ACCOUNT_ID"):
            env_overrides["account_id"] = values["CHAT_AUDIT_WECHAT_ACCOUNT_ID"]
        if values.get("CHAT_AUDIT_WECHAT_ACCOUNT_NAME"):
            env_overrides["account_name"] = values["CHAT_AUDIT_WECHAT_ACCOUNT_NAME"]
        if env_overrides:
            config = config.with_overrides(env_overrides)
        return config

    def with_overrides(self, raw: Mapping[str, Any]) -> "AdapterConfig":
        data = dict(raw)
        if "queue_db" in data and data["queue_db"] is not None:
            data["queue_db"] = Path(str(data["queue_db"]))
        if "log_dir" in data and data["log_dir"] is not None:
            data["log_dir"] = Path(str(data["log_dir"]))
        return replace(self, **{key: value for key, value in data.items() if hasattr(self, key)})

    @property
    def normalized_nas_url(self) -> str:
        return self.nas_url.rstrip("/")

    def ensure_dirs(self) -> None:
        if self.queue_db is not None:
            self.queue_db.parent.mkdir(parents=True, exist_ok=True)
        if self.log_dir is not None:
            self.log_dir.mkdir(parents=True, exist_ok=True)
