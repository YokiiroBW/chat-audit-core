from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from collector.qqnt.snapshot import detect_database_format, is_plain_sqlite
from collector.qqnt.capabilities import detect_runtime_capabilities
from collector.qqnt.sqlcipher import QQNT_DATABASE_KEY_NAME, sqlcipher_available


@dataclass(frozen=True)
class KeyValidationResult:
    status: str
    error_code: str | None = None
    detail: str | None = None

    @property
    def usable(self) -> bool:
        return self.status in {"valid", "not_required"}


class CredentialKeyProvider:
    def __init__(self, getter: Callable[[str], str | None]) -> None:
        self.getter = getter

    def get_database_key(self) -> str | None:
        return self.getter(QQNT_DATABASE_KEY_NAME)


class SQLiteKeyValidator:
    def validate(self, database_path: str | Path, key: str | None) -> KeyValidationResult:
        path = Path(database_path).expanduser().resolve()
        if not path.is_file():
            return KeyValidationResult("invalid", "DB_OPEN_FAILED", "database file does not exist")
        database_format = detect_database_format(path)
        if database_format == "qqnt_custom_vfs":
            if not sqlcipher_available():
                capabilities = detect_runtime_capabilities()
                detail = "QQNT custom database detected, but sqlcipher3 is not installed"
                if capabilities.sqlcipher_available:
                    detail = f"SQLCipher runtime {capabilities.sqlcipher_provider} is unavailable to the Collector"
                return KeyValidationResult("unsupported", "DB_SQLCIPHER_UNAVAILABLE", detail)
            if not key:
                return KeyValidationResult("invalid", "DB_KEY_INVALID", "QQNT database key is required")
            if len(key.encode("utf-8")) != 16:
                return KeyValidationResult("invalid", "DB_KEY_INVALID", "QQNT database key must be exactly 16 bytes")
            return KeyValidationResult("valid")
        if is_plain_sqlite(path):
            try:
                uri = f"file:{path.as_posix()}?mode=ro"
                with sqlite3.connect(uri, uri=True, timeout=5) as connection:
                    connection.execute("PRAGMA query_only=ON")
                    connection.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
            except sqlite3.Error:
                return KeyValidationResult("invalid", "DB_OPEN_FAILED", "plain SQLite database cannot be read")
            return KeyValidationResult("not_required")
        try:
            uri = f"file:{path.as_posix()}?mode=ro"
            with sqlite3.connect(uri, uri=True, timeout=5) as connection:
                cipher_row = connection.execute("PRAGMA cipher_version").fetchone()
                cipher_version = cipher_row[0] if cipher_row else None
                if not cipher_version:
                    return KeyValidationResult(
                        "unsupported",
                        "DB_SCHEMA_UNSUPPORTED",
                        "the bundled SQLite runtime has no SQLCipher/NTQQ VFS support",
                    )
                if not key:
                    return KeyValidationResult("invalid", "DB_KEY_INVALID", "database key is required")
                key_hex = key.encode("utf-8").hex()
                connection.execute(f"PRAGMA key=\"x'{key_hex}'\"")
                connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
                return KeyValidationResult("valid")
        except sqlite3.Error:
            return KeyValidationResult("invalid", "DB_KEY_INVALID", "configured database key was rejected")
