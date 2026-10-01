from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from collector.qqnt.reader import ReadOnlySQLiteDatabase
from collector.qqnt.snapshot import DatabaseSnapshotManager, detect_database_format
from collector.qqnt.sqlcipher import materialize_clear_database


@dataclass
class QQNTProfileIndex:
    sender_nicknames: dict[str, str] = field(default_factory=dict)
    room_names: dict[str, str] = field(default_factory=dict)
    sender_avatars: dict[str, str] = field(default_factory=dict)
    room_avatars: dict[str, str] = field(default_factory=dict)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _identifier(value: Any) -> str | None:
    text = _text(value)
    return text


def _avatar_value(row: Any) -> str | None:
    values = row.keys() if hasattr(row, "keys") else ()
    for key in values:
        key_text = str(key).lower()
        value = _text(row[key])
        if not value or ("avatar" not in key_text and "head" not in key_text and "logo" not in key_text):
            continue
        if value.startswith(("http://", "https://", "/", "\\")):
            return _normalize_avatar_url(value)
    for key in values:
        value = _text(row[key])
        if value and ("qlogo" in value.lower() or "head_image" in value.lower()):
            return _normalize_avatar_url(value)
    return None


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _open_snapshot(
    path: Path,
    *,
    key: str | None,
    snapshots: DatabaseSnapshotManager,
) -> tuple[ReadOnlySQLiteDatabase, Any]:
    snapshot = snapshots.create(path)
    target = snapshot.database_path
    cipher = detect_database_format(path) == "qqnt_custom_vfs"
    if cipher:
        target = materialize_clear_database(target, snapshot.snapshot_dir / f"{path.stem}.clear.db")
    return ReadOnlySQLiteDatabase(target, immutable=not cipher, key=key, cipher=cipher), snapshot


def _load_group_info(database: ReadOnlySQLiteDatabase, index: QQNTProfileIndex) -> None:
    with database.read_transaction() as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "group_list" in tables:
            for row in connection.execute(f"SELECT * FROM {_quote('group_list')}"):
                room_id = _identifier(row["60001"])
                name = _text(row["60007"])
                if room_id and name:
                    index.room_names[room_id] = name
                avatar = _avatar_value(row)
                if room_id and avatar:
                    index.room_avatars[room_id] = avatar
        if "group_member3" in tables:
            for row in connection.execute(f"SELECT * FROM {_quote('group_member3')}"):
                member_id = _identifier(row["1000"])
                nickname = _text(row["64003"]) or _text(row["20002"])
                if member_id and nickname:
                    index.sender_nicknames[member_id] = nickname
                avatar = _avatar_value(row)
                if member_id and avatar:
                    index.sender_avatars[member_id] = avatar


def _load_profile_info(database: ReadOnlySQLiteDatabase, index: QQNTProfileIndex) -> None:
    with database.read_transaction() as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "profile_info_v6" not in tables:
            return
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({_quote('profile_info_v6')})")}
        if "1000" not in columns:
            return
        name_column = "20009" if "20009" in columns else "20002" if "20002" in columns else None
        if not name_column:
            return
        for row in connection.execute(f"SELECT * FROM {_quote('profile_info_v6')}"):
            user_id = _identifier(row["1000"])
            nickname = _text(row[name_column])
            if user_id and nickname and user_id not in index.sender_nicknames:
                index.sender_nicknames[user_id] = nickname
            avatar = _avatar_value(row)
            if user_id and avatar and user_id not in index.sender_avatars:
                index.sender_avatars[user_id] = avatar


def load_profile_index(
    database_paths: tuple[Path, ...],
    *,
    key: str | None,
    snapshots: DatabaseSnapshotManager,
) -> QQNTProfileIndex:
    index = QQNTProfileIndex()
    parents = {path.parent for path in database_paths}
    for parent in parents:
        group_info = parent / "group_info.db"
        if group_info.is_file():
            snapshot = None
            try:
                database, snapshot = _open_snapshot(group_info, key=key, snapshots=snapshots)
                _load_group_info(database, index)
            except Exception:
                pass
            finally:
                if snapshot is not None:
                    snapshots.cleanup(snapshot)
        profile_info = parent / "profile_info.db"
        if profile_info.is_file():
            snapshot = None
            try:
                database, snapshot = _open_snapshot(profile_info, key=key, snapshots=snapshots)
                _load_profile_info(database, index)
            except Exception:
                pass
            finally:
                if snapshot is not None:
                    snapshots.cleanup(snapshot)
    return index
