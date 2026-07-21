from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping


PREFERRED_DATABASE_NAMES = frozenset(
    {
        "nt_msg.db",
        "group_msg.db",
        "c2c_msg.db",
        "guild_msg.db",
        "contact.db",
    }
)


@dataclass(frozen=True)
class QQNTDatabaseFile:
    path: Path
    wal_path: Path | None
    shm_path: Path | None
    size: int
    modified_ns: int
    role: str


@dataclass(frozen=True)
class QQNTDataSet:
    root: Path
    databases: tuple[QQNTDatabaseFile, ...]


def candidate_data_roots(
    *,
    configured_root: str | Path | None = None,
    account_id: str | None = None,
    environment: Mapping[str, str] | None = None,
) -> list[Path]:
    env = os.environ if environment is None else environment
    candidates: list[Path] = []
    if configured_root:
        configured = Path(configured_root).expanduser().resolve()
        return [configured] if configured.is_dir() else []
    local_app_data = env.get("LOCALAPPDATA")
    roaming_app_data = env.get("APPDATA")
    user_profile = env.get("USERPROFILE")
    if local_app_data:
        candidates.extend(
            [
                Path(local_app_data) / "Tencent" / "QQ",
                Path(local_app_data) / "Tencent" / "QQNT",
            ]
        )
    if roaming_app_data:
        candidates.extend(
            [
                Path(roaming_app_data) / "Tencent" / "QQ",
                Path(roaming_app_data) / "Tencent" / "QQNT",
            ]
        )
    if user_profile:
        documents = Path(user_profile) / "Documents"
        if account_id:
            candidates.extend(
                [
                    documents / "Tencent Files" / account_id,
                    documents / "QQ Files" / account_id,
                ]
            )
        candidates.extend([documents / "Tencent Files", documents / "QQ Files"])

    unique: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.is_dir():
            unique.append(resolved)
    return unique


def _database_role(path: Path) -> str:
    name = path.name.lower()
    if "group" in name:
        return "group_message"
    if "c2c" in name or "friend" in name or "private" in name:
        return "private_message"
    if "msg" in name:
        return "message"
    if "contact" in name or "buddy" in name:
        return "contact"
    return "unknown"


def _iter_database_candidates(root: Path, *, max_depth: int) -> Iterable[Path]:
    root_depth = len(root.parts)
    for current_root, directories, files in os.walk(root):
        current = Path(current_root)
        depth = len(current.parts) - root_depth
        directories[:] = [
            directory
            for directory in directories
            if directory.lower() not in {"cache", "caches", "log", "logs", "temp", "tmp", "staging"}
        ]
        if depth >= max_depth:
            directories[:] = []
        lowered_parts = {part.lower() for part in current.parts[-4:]}
        likely_database_area = bool(lowered_parts & {"database", "databases", "db", "msg", "nt_qq", "nt_data"})
        for file_name in files:
            lowered = file_name.lower()
            if not lowered.endswith((".db", ".sqlite", ".sqlite3")):
                continue
            if lowered in PREFERRED_DATABASE_NAMES or "msg" in lowered or likely_database_area:
                yield current / file_name


def discover_database_sets(root: str | Path, *, max_depth: int = 6) -> list[QQNTDataSet]:
    data_root = Path(root).expanduser().resolve()
    if not data_root.is_dir():
        return []
    databases: list[QQNTDatabaseFile] = []
    for path in sorted(set(_iter_database_candidates(data_root, max_depth=max_depth))):
        try:
            stat = path.stat()
        except OSError:
            continue
        wal = Path(str(path) + "-wal")
        shm = Path(str(path) + "-shm")
        databases.append(
            QQNTDatabaseFile(
                path=path,
                wal_path=wal if wal.is_file() else None,
                shm_path=shm if shm.is_file() else None,
                size=stat.st_size,
                modified_ns=stat.st_mtime_ns,
                role=_database_role(path),
            )
        )
    return [QQNTDataSet(data_root, tuple(databases))] if databases else []


def discover_qqnt_data(
    *,
    configured_root: str | Path | None = None,
    account_id: str | None = None,
    environment: Mapping[str, str] | None = None,
    max_depth: int = 6,
) -> list[QQNTDataSet]:
    discovered: list[QQNTDataSet] = []
    for root in candidate_data_roots(
        configured_root=configured_root,
        account_id=account_id,
        environment=environment,
    ):
        discovered.extend(discover_database_sets(root, max_depth=max_depth))
    return discovered
