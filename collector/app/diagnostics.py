from __future__ import annotations

import hashlib
import json
import platform
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from collector import __version__
from collector.app.config import CollectorConfig
from collector.app.logging import redact
from collector.qqnt.discovery import discover_qqnt_data
from collector.qqnt.snapshot import detect_database_format
from collector.sync.state import CollectorStateStore


def _json_bytes(value: Any) -> bytes:
    return json.dumps(redact(value), ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")


def _account_fingerprint(account_id: str) -> str:
    return hashlib.sha256(account_id.encode("utf-8")).hexdigest()[:12]


def _server_origin(base_url: str) -> str:
    parsed = urlsplit(base_url)
    return f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else "invalid"


def _log_tail(log_dir: Path, *, max_bytes: int = 512 * 1024) -> str:
    path = log_dir / "collector.log"
    if not path.is_file():
        return ""
    with path.open("rb") as file:
        size = path.stat().st_size
        file.seek(max(0, size - max_bytes))
        content = file.read().decode("utf-8", errors="replace")
    return str(redact(content))


def create_diagnostic_bundle(
    config: CollectorConfig,
    store: CollectorStateStore,
    *,
    output_path: str | Path | None = None,
) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    destination = (
        Path(output_path).expanduser().resolve()
        if output_path is not None
        else (config.paths.root / "diagnostics" / f"collector-diagnostics-{timestamp}.zip").resolve()
    )
    destination.parent.mkdir(parents=True, exist_ok=True)

    discovery = []
    for data_set in discover_qqnt_data(
        configured_root=config.qq.data_root,
        account_id=config.collector.account_id,
    ):
        discovery.append(
            {
                "root": str(redact(str(data_set.root))),
                "databases": [
                    {
                        "name": database.path.name,
                        "role": database.role,
                        "format": detect_database_format(database.path),
                        "size": database.size,
                        "has_wal": database.wal_path is not None,
                        "has_shm": database.shm_path is not None,
                    }
                    for database in data_set.databases
                ],
            }
        )

    manifest = {
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "collector_version": __version__,
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "executable": str(redact(sys.executable)),
        "account_fingerprint": _account_fingerprint(config.collector.account_id),
        "device_id": config.collector.device_id,
        "read_mode": config.qq.read_mode,
        "server_origin": _server_origin(config.server.base_url),
        "state_schema_version": store.get_meta("schema_version"),
        "initial_scan_completed": store.get_meta("initial_scan_completed") == "1",
        "discovery": discovery,
        "excluded": [
            "credentials.dpapi",
            "QQNT database contents",
            "message payloads",
            "staged media",
        ],
    }
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", _json_bytes(manifest))
        archive.writestr("status.json", _json_bytes(store.status_snapshot()))
        log_tail = _log_tail(config.paths.log_dir)
        if log_tail:
            archive.writestr("collector.log.tail.jsonl", log_tail.encode("utf-8"))
    return destination


__all__ = ["create_diagnostic_bundle"]
