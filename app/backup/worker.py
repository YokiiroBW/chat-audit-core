from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import resource
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from app.backup.exporter import create_full_backup_archive
from app.backup.jobs import (
    JOB_DIRECTORY,
    backup_execution_lock,
    claim_next_backup_job,
    finish_backup_job,
    mark_interrupted_jobs_failed,
    write_worker_heartbeat,
)
from app.config import Settings, get_settings
from app.services.backup_service import BackupService
from app.time_utils import parse_utc_datetime, utc_now


logger = logging.getLogger(__name__)


def completed_auto_backups(backup_root: Path) -> list[Path]:
    root = Path(backup_root)
    candidates = [
        path
        for pattern in ("auto-backup-*.cacb",)
        for path in root.glob(pattern)
        if path.is_file() and not path.name.startswith(".")
    ]
    return sorted(candidates, key=lambda path: (path.stat().st_mtime_ns, path.name))


def apply_auto_backup_retention(backup_root: Path, keep_latest: int) -> list[Path]:
    """Delete only excess v4 auto backups; preserve legacy, manual and failed files."""

    keep = max(0, int(keep_latest))
    if keep == 0:
        return []
    backups = completed_auto_backups(backup_root)
    removed: list[Path] = []
    for path in backups[:-keep]:
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        removed.append(path)
    return removed


def _peak_rss_mib() -> float:
    # Linux reports ru_maxrss in KiB. This image is Linux-only and the cgroup
    # contract is part of the production deployment.
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def _install_process_memory_limit(settings: Settings) -> None:
    limit = int(settings.backup_worker_address_space_limit_bytes)
    if limit <= 0:
        raise ValueError("BACKUP_WORKER_ADDRESS_SPACE_LIMIT_BYTES must be positive")
    current_soft, current_hard = resource.getrlimit(resource.RLIMIT_AS)
    hard = limit if current_hard == resource.RLIM_INFINITY else min(limit, current_hard)
    soft = min(limit, hard)
    resource.setrlimit(resource.RLIMIT_AS, (soft, hard))


@contextmanager
def _heartbeat_during_job(
    backup_root: Path,
    *,
    max_age_seconds: int,
    interval_seconds: float | None = None,
) -> Iterator[None]:
    interval = interval_seconds if interval_seconds is not None else min(30.0, max(1.0, max_age_seconds / 3))
    stopped = threading.Event()

    def heartbeat_loop() -> None:
        while not stopped.wait(interval):
            try:
                write_worker_heartbeat(backup_root)
            except OSError:
                logger.exception("Backup worker could not refresh its heartbeat")

    write_worker_heartbeat(backup_root)
    thread = threading.Thread(target=heartbeat_loop, name="backup-worker-heartbeat", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join(timeout=max(1.0, interval * 2))
        with contextlib.suppress(OSError):
            write_worker_heartbeat(backup_root)


async def run_one_job(settings: Settings) -> dict[str, Any] | None:
    claimed = claim_next_backup_job(settings.backup_root)
    if claimed is None:
        return None
    started = time.monotonic()
    request = claimed.request
    try:
        with _heartbeat_during_job(
            settings.backup_root,
            max_age_seconds=int(settings.backup_worker_heartbeat_max_age_seconds),
        ):
            with backup_execution_lock(settings.backup_root):
                result = await create_full_backup_archive(
                    database_url=settings.database_url,
                    storage_root=settings.storage_root,
                    backup_root=settings.backup_root,
                    public_storage_prefix=settings.public_storage_prefix,
                    signing_key=settings.app_secret_key,
                    system_id=settings.system_instance_id,
                    backup_type=str(request["backup_type"]),
                    created_by=str(request["created_by"]),
                    chunk_bytes=settings.backup_chunk_bytes,
                    min_free_bytes=settings.backup_min_free_bytes,
                )
                removed: list[Path] = []
                if request["backup_type"] == "auto":
                    removed = apply_auto_backup_retention(
                        settings.backup_root,
                        int(request.get("keep_latest") or 0),
                    )
        elapsed = time.monotonic() - started
        status = finish_backup_job(
            settings.backup_root,
            claimed,
            state="succeeded",
            result={
                "filename": result.path.name,
                "path": str(result.path),
                "size_bytes": result.path.stat().st_size,
                "duration_seconds": round(elapsed, 3),
                "peak_rss_mib": round(_peak_rss_mib(), 3),
                "counts": result.manifest.get("counts", {}),
                "retention_removed": [path.name for path in removed],
            },
        )
        logger.info(
            "Backup worker completed a job",
            extra={
                "job_id": claimed.job_id,
                "backup_type": request.get("backup_type"),
                "backup_filename": result.path.name,
                "size_bytes": result.path.stat().st_size,
                "duration_seconds": elapsed,
                "peak_rss_mib": _peak_rss_mib(),
            },
        )
        return status
    except BaseException as exc:
        elapsed = time.monotonic() - started
        status = finish_backup_job(
            settings.backup_root,
            claimed,
            state="failed",
            result={
                "duration_seconds": round(elapsed, 3),
                "peak_rss_mib": round(_peak_rss_mib(), 3),
            },
            error=f"{type(exc).__name__}: {exc}",
        )
        BackupService.write_failure_log(
            settings.backup_root,
            event="backup_worker",
            error=f"{type(exc).__name__}: {exc}",
            context={"job_id": claimed.job_id, "backup_type": request.get("backup_type")},
        )
        logger.exception(
            "Backup worker job failed",
            extra={"job_id": claimed.job_id, "backup_type": request.get("backup_type")},
        )
        return status


async def worker_loop(settings: Settings) -> None:
    mark_interrupted_jobs_failed(settings.backup_root)
    while True:
        write_worker_heartbeat(settings.backup_root)
        await run_one_job(settings)
        await asyncio.sleep(max(0.1, float(settings.backup_worker_poll_seconds)))


def worker_is_healthy(settings: Settings) -> bool:
    heartbeat = Path(settings.backup_root) / JOB_DIRECTORY / "worker-heartbeat.json"
    try:
        import json

        record = json.loads(heartbeat.read_text(encoding="utf-8"))
        updated_at = parse_utc_datetime(record["updated_at"])
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return False
    age = (utc_now().replace(tzinfo=None) - updated_at.replace(tzinfo=None)).total_seconds()
    return 0 <= age <= int(settings.backup_worker_heartbeat_max_age_seconds)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="chat-audit-core bounded backup worker")
    parser.add_argument("command", choices=("run", "once", "healthcheck"), nargs="?", default="run")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    settings = get_settings()
    if args.command == "healthcheck":
        return 0 if worker_is_healthy(settings) else 1
    _install_process_memory_limit(settings)
    if args.command == "once":
        status = asyncio.run(run_one_job(settings))
        return 0 if status is None or status.get("state") == "succeeded" else 1
    asyncio.run(worker_loop(settings))
    return 0


if __name__ == "__main__":
    sys.exit(main())
