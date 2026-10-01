from __future__ import annotations

import hashlib
import shutil
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path


class SnapshotError(RuntimeError):
    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


@dataclass(frozen=True)
class DatabaseSnapshot:
    source_path: Path
    database_path: Path
    snapshot_dir: Path
    method: str
    content_sha256: str


def sqlite_header(path: str | Path) -> bytes:
    with Path(path).open("rb") as file:
        return file.read(16)


def detect_database_format(path: str | Path) -> str:
    try:
        source = Path(path)
        with source.open("rb") as file:
            header = file.read(64)
    except OSError:
        return "missing"
    if header[:16] == b"SQLite format 3\x00":
        return "sqlite"
    if header[:16] == b"SQLite header 3\x00" and b"QQ_NT DB" in header[16:64]:
        return "qqnt_custom_vfs"
    return "unknown"


def is_plain_sqlite(path: str | Path) -> bool:
    return detect_database_format(path) == "sqlite"


class DatabaseSnapshotManager:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as file:
            while chunk := file.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    def _snapshot_dir(self) -> Path:
        path = self.root / f"snapshot-{int(time.time())}-{uuid.uuid4().hex}"
        path.mkdir(parents=True, exist_ok=False)
        return path

    def snapshot_plain_sqlite(self, source_path: str | Path) -> DatabaseSnapshot:
        source = Path(source_path).expanduser().resolve()
        if not source.is_file() or not is_plain_sqlite(source):
            raise SnapshotError("DB_OPEN_FAILED", "database is not a readable plain SQLite file")
        snapshot_dir = self._snapshot_dir()
        destination = snapshot_dir / source.name
        source_uri = f"file:{source.as_posix()}?mode=ro"
        try:
            with sqlite3.connect(source_uri, uri=True, timeout=30) as source_db:
                source_db.execute("PRAGMA query_only=ON")
                with sqlite3.connect(destination) as destination_db:
                    source_db.backup(destination_db)
        except sqlite3.Error as exc:
            shutil.rmtree(snapshot_dir, ignore_errors=True)
            raise SnapshotError("DB_SNAPSHOT_FAILED", "SQLite backup snapshot failed") from exc
        return DatabaseSnapshot(source, destination, snapshot_dir, "sqlite_backup", self._sha256(destination))

    def snapshot_file_family(self, source_path: str | Path, *, retries: int = 3) -> DatabaseSnapshot:
        source = Path(source_path).expanduser().resolve()
        if not source.is_file():
            raise SnapshotError("DB_OPEN_FAILED", "database file does not exist")
        def current_family() -> list[Path]:
            family = [source]
            for suffix in ("-wal", "-shm"):
                companion = Path(str(source) + suffix)
                if companion.is_file():
                    family.append(companion)
            return family

        for _ in range(max(1, retries)):
            snapshot_dir = self._snapshot_dir()
            try:
                family_before = current_family()
                before = {path: (path.stat().st_size, path.stat().st_mtime_ns) for path in family_before}
                for path in family_before:
                    shutil.copy2(path, snapshot_dir / path.name)
                family_after = current_family()
                after = {path: (path.stat().st_size, path.stat().st_mtime_ns) for path in family_after}
                copied = {
                    path: ((snapshot_dir / path.name).stat().st_size, (snapshot_dir / path.name).stat().st_mtime_ns)
                    for path in family_before
                }
            except OSError:
                shutil.rmtree(snapshot_dir, ignore_errors=True)
                continue
            if family_before == family_after and before == after == copied:
                destination = snapshot_dir / source.name
                return DatabaseSnapshot(source, destination, snapshot_dir, "verified_family_copy", self._sha256(destination))
            shutil.rmtree(snapshot_dir, ignore_errors=True)
            time.sleep(0.05)
        raise SnapshotError("DB_READ_INCONSISTENT", "database family changed while it was being copied")

    def create(self, source_path: str | Path) -> DatabaseSnapshot:
        if is_plain_sqlite(source_path):
            return self.snapshot_plain_sqlite(source_path)
        return self.snapshot_file_family(source_path)

    def cleanup(self, snapshot: DatabaseSnapshot) -> None:
        resolved = snapshot.snapshot_dir.resolve()
        if self.root != resolved and self.root not in resolved.parents:
            raise SnapshotError("DB_SNAPSHOT_FAILED", "refusing to delete a snapshot outside the snapshot root")
        shutil.rmtree(resolved, ignore_errors=True)
