from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any


_BEARER_PATTERN = re.compile(r"(?i)bearer\s+[a-z0-9._~+/=-]+")
_USER_PATH_PATTERN = re.compile(r"(?i)([a-z]:\\users\\)[^\\\s]+")
_SENSITIVE_KEYS = frozenset({"api_token", "database_key", "db_key", "password", "secret", "token"})


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: "[REDACTED]" if str(key).lower() in _SENSITIVE_KEYS else redact(nested) for key, nested in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    if isinstance(value, str):
        sanitized = _BEARER_PATTERN.sub("Bearer [REDACTED]", value)
        return _USER_PATH_PATTERN.sub(r"\1[USER]", sanitized)
    return value


class CollectorJsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        for key in ("event", "batch_id", "error_code", "message_count", "media_state", "elapsed_ms"):
            if hasattr(record, key):
                payload[key] = redact(getattr(record, key))
        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def configure_logging(log_dir: str | Path, *, diagnostic: bool = False) -> None:
    directory = Path(log_dir).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    formatter = CollectorJsonFormatter()
    root = logging.getLogger("collector")
    for handler in root.handlers:
        handler.close()
    root.handlers.clear()
    root.setLevel(logging.DEBUG if diagnostic else logging.INFO)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    file_handler = RotatingFileHandler(
        directory / "collector.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)
    root.propagate = False
