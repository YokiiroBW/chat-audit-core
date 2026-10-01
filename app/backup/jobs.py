from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from app.atomic_io import atomic_write_text
from app.time_utils import format_utc_z, utc_now


JOB_DIRECTORY = ".backup-jobs"
JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")
ACTIVE_STATES = frozenset({"queued", "running"})
FINAL_STATES = frozenset({"succeeded", "failed"})
MAX_JOB_ERROR_LENGTH = 1000
COMPLETED_BACKUP_PATTERNS = (
    "auto-backup-*.cacb",
    "manual-backup-*.cacb",
    "converted-backup-*.cacb",
    "auto-backup-*.json",
    "auto-backup-*.json.gz",
)


class BackupAlreadyRunningError(RuntimeError):
    def __init__(self, job: dict[str, Any]):
        self.job = job
        super().__init__(f"backup job {job.get('job_id')} is already {job.get('state')}")


@dataclass(frozen=True)
class ClaimedBackupJob:
    job_id: str
    request: dict[str, Any]
    claimed_path: Path


def _job_root(backup_root: Path) -> Path:
    root = Path(backup_root) / JOB_DIRECTORY
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def _request_path(root: Path, job_id: str) -> Path:
    return root / f"{job_id}.request.json"


def _claimed_path(root: Path, job_id: str) -> Path:
    return root / f"{job_id}.claimed.json"


def _status_path(root: Path, job_id: str) -> Path:
    return root / f"{job_id}.status.json"


def _finished_request_path(root: Path, job_id: str, state: str) -> Path:
    return root / f"{job_id}.{state}.request.json"


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"backup job file must contain an object: {path.name}")
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")


@contextlib.contextmanager
def _queue_lock(root: Path, *, blocking: bool = True) -> Iterator[None]:
    lock_path = root / "queue.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        operation = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        fcntl.flock(descriptor, operation)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


@contextlib.contextmanager
def backup_execution_lock(backup_root: Path, *, blocking: bool = False) -> Iterator[None]:
    root = _job_root(backup_root)
    lock_path = root / "backup-execution.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        operation = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(descriptor, operation)
        except BlockingIOError as exc:
            raise BackupAlreadyRunningError(active_backup_job(backup_root) or {"state": "running"}) from exc
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def list_job_statuses(backup_root: Path, *, limit: int = 100) -> list[dict[str, Any]]:
    root = _job_root(backup_root)
    statuses: list[dict[str, Any]] = []
    for path in root.glob("*.status.json"):
        try:
            status = _read_json(path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        statuses.append(status)
    statuses.sort(key=lambda item: (str(item.get("created_at") or ""), str(item.get("job_id") or "")), reverse=True)
    return statuses[: max(0, int(limit))]


def list_completed_backups(backup_root: Path) -> list[Path]:
    root = Path(backup_root)
    if not root.exists():
        return []
    paths = {
        path
        for pattern in COMPLETED_BACKUP_PATTERNS
        for path in root.glob(pattern)
        if path.is_file() and not path.name.startswith(".")
    }
    return sorted(paths, key=lambda path: (path.stat().st_mtime_ns, path.name))


def active_backup_job(backup_root: Path) -> dict[str, Any] | None:
    return next((job for job in list_job_statuses(backup_root) if job.get("state") in ACTIVE_STATES), None)


def get_backup_job(backup_root: Path, job_id: str) -> dict[str, Any] | None:
    if not JOB_ID_RE.fullmatch(job_id):
        return None
    path = _status_path(_job_root(backup_root), job_id)
    if not path.is_file():
        return None
    try:
        return _read_json(path)
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def enqueue_backup_job(
    backup_root: Path,
    *,
    backup_type: str,
    created_by: str,
    keep_latest: int,
) -> dict[str, Any]:
    if backup_type not in {"auto", "manual"}:
        raise ValueError(f"unsupported queued backup type: {backup_type!r}")
    root = _job_root(backup_root)
    with _queue_lock(root):
        active = active_backup_job(backup_root)
        if active is not None:
            raise BackupAlreadyRunningError(active)
        job_id = secrets.token_hex(16)
        created_at = format_utc_z(utc_now())
        request = {
            "job_id": job_id,
            "operation": "backup",
            "backup_type": backup_type,
            "created_by": created_by,
            "keep_latest": max(0, int(keep_latest)),
            "created_at": created_at,
        }
        status = {
            "job_id": job_id,
            "operation": "backup",
            "backup_type": backup_type,
            "state": "queued",
            "created_at": created_at,
            "updated_at": created_at,
        }
        # The worker shares this lock, so publishing the request first cannot
        # race a claim. If the second write fails, the worker can reconstruct a
        # status from the durable request; the opposite order could leave a
        # permanently "queued" status with no work item behind it.
        _write_json(_request_path(root, job_id), request)
        _write_json(_status_path(root, job_id), status)
        return status


def claim_next_backup_job(backup_root: Path) -> ClaimedBackupJob | None:
    root = _job_root(backup_root)
    with _queue_lock(root):
        request_paths = sorted(root.glob("*.request.json"), key=lambda path: (path.stat().st_mtime_ns, path.name))
        for path in request_paths:
            if ".succeeded.request.json" in path.name or ".failed.request.json" in path.name:
                continue
            job_id = path.name.removesuffix(".request.json")
            if not JOB_ID_RE.fullmatch(job_id):
                continue
            request = _read_json(path)
            claimed = _claimed_path(root, job_id)
            try:
                os.replace(path, claimed)
            except FileNotFoundError:
                continue
            status = get_backup_job(backup_root, job_id) or {
                "job_id": job_id,
                "operation": "backup",
                "backup_type": request.get("backup_type"),
                "created_at": request.get("created_at"),
            }
            now = format_utc_z(utc_now())
            status.update(
                {
                    "state": "running",
                    "started_at": now,
                    "updated_at": now,
                    "worker_pid": os.getpid(),
                }
            )
            _write_json(_status_path(root, job_id), status)
            return ClaimedBackupJob(job_id=job_id, request=request, claimed_path=claimed)
    return None


def finish_backup_job(
    backup_root: Path,
    claimed: ClaimedBackupJob,
    *,
    state: str,
    result: dict[str, Any] | None = None,
    error: BaseException | str | None = None,
) -> dict[str, Any]:
    if state not in FINAL_STATES:
        raise ValueError(f"invalid final backup job state: {state!r}")
    root = _job_root(backup_root)
    with _queue_lock(root):
        status = get_backup_job(backup_root, claimed.job_id) or {
            "job_id": claimed.job_id,
            "operation": "backup",
            "backup_type": claimed.request.get("backup_type"),
            "created_at": claimed.request.get("created_at"),
        }
        now = format_utc_z(utc_now())
        status.update({"state": state, "finished_at": now, "updated_at": now})
        status.pop("worker_pid", None)
        if result:
            status.update(result)
        if error is not None:
            error_text = str(error).replace("\x00", "?")
            status["error"] = (
                error_text
                if len(error_text) <= MAX_JOB_ERROR_LENGTH
                else error_text[: MAX_JOB_ERROR_LENGTH - 3] + "..."
            )
        _write_json(_status_path(root, claimed.job_id), status)
        if claimed.claimed_path.exists():
            os.replace(claimed.claimed_path, _finished_request_path(root, claimed.job_id, state))
        return status


def mark_interrupted_jobs_failed(backup_root: Path) -> int:
    """Make a worker crash visible without silently retrying the same full backup."""

    root = _job_root(backup_root)
    marked = 0
    with _queue_lock(root):
        for claimed_path in root.glob("*.claimed.json"):
            job_id = claimed_path.name.removesuffix(".claimed.json")
            if not JOB_ID_RE.fullmatch(job_id):
                continue
            request = _read_json(claimed_path)
            status = get_backup_job(backup_root, job_id) or {
                "job_id": job_id,
                "operation": "backup",
                "backup_type": request.get("backup_type"),
                "created_at": request.get("created_at"),
            }
            now = format_utc_z(utc_now())
            status.update(
                {
                    "state": "failed",
                    "finished_at": now,
                    "updated_at": now,
                    "error": "backup worker stopped before the job completed",
                }
            )
            status.pop("worker_pid", None)
            _write_json(_status_path(root, job_id), status)
            os.replace(claimed_path, _finished_request_path(root, job_id, "failed"))
            marked += 1
    return marked


def write_worker_heartbeat(backup_root: Path) -> Path:
    root = _job_root(backup_root)
    path = root / "worker-heartbeat.json"
    _write_json(path, {"pid": os.getpid(), "updated_at": format_utc_z(utc_now())})
    return path
