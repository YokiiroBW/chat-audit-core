import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

try:
    from pythonjsonlogger import jsonlogger
except ImportError:  # pragma: no cover - exercised when optional dependency is absent.
    jsonlogger = None


_STANDARD_LOG_RECORD_FIELDS = set(
    logging.LogRecord(
        name="",
        level=0,
        pathname="",
        lineno=0,
        msg="",
        args=(),
        exc_info=None,
    ).__dict__
)


class FallbackJsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "asctime": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "name": record.name,
            "levelname": record.levelname,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_LOG_RECORD_FIELDS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def _formatter() -> logging.Formatter:
    if jsonlogger is not None:
        return jsonlogger.JsonFormatter("%(asctime)s %(name)s %(levelname)s %(message)s")
    return FallbackJsonFormatter()


def setup_logging(log_level: str = "INFO") -> None:
    level = getattr(logging, str(log_level or "INFO").upper(), logging.INFO)
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    for handler in root_logger.handlers:
        if getattr(handler, "_chat_audit_json_handler", False):
            handler.setLevel(level)
            handler.setFormatter(_formatter())
            return

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)
    handler.setFormatter(_formatter())
    handler._chat_audit_json_handler = True  # type: ignore[attr-defined]
    root_logger.addHandler(handler)
