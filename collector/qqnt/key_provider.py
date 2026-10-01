from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from collector.qqnt.snapshot import detect_database_format, is_plain_sqlite
from collector.qqnt.capabilities import detect_runtime_capabilities
from collector.qqnt.sqlcipher import QQNT_DATABASE_KEY_NAME, body_looks_encrypted, sqlcipher_available


@dataclass(frozen=True)
class KeyValidationResult:
    """Outcome of the cheap, non-destructive pre-check on a QQNT database file.

    Statuses:
      * ``not_required`` — plain SQLite, opened read-only and proven readable here.
      * ``valid``        — proven readable here (a real open succeeded).
      * ``unverified``   — every cheap gate passed, but readability is NOT proven. Only
                           opening a stripped copy can prove it, and that costs a full-file
                           copy (a real QQNT message database is hundreds of MB), so it is
                           deliberately left to the caller that materialises the copy anyway.
      * ``invalid`` / ``unsupported`` — cannot be read; see ``error_code``.
    """

    status: str
    error_code: str | None = None
    detail: str | None = None

    @property
    def usable(self) -> bool:
        """Readability has actually been proven. Never true on a key-length check alone."""
        return self.status in {"valid", "not_required"}

    @property
    def may_attempt(self) -> bool:
        """Cheap gates passed, so opening the database is worth attempting.

        Callers that gate on this must treat the subsequent real open as the authority and
        surface its failure, rather than reporting the database as readable up front.
        """
        return self.usable or self.status == "unverified"

    @property
    def requires_key(self) -> bool:
        """Whether the configured key must be handed to the connection when opening."""
        return self.status in {"valid", "unverified"}


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
            # Gate order matters. The key gates come first so that a user who simply has not
            # entered the key yet gets the actionable "needs key" answer instead of a blanket
            # "unsupported"; DB_VFS_UNSUPPORTED is reserved for bodies that genuinely cannot
            # be stripped and decrypted at all.
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
            if not body_looks_encrypted(path):
                return KeyValidationResult(
                    "unsupported",
                    "DB_VFS_UNSUPPORTED",
                    "QQNT custom header is not followed by a SQLCipher body, so no compatible "
                    "header strip exists for this database",
                )
            # Every cheap gate passed. Readability is still unproven: a 16-byte key is only a
            # well-formed key, not a correct one. Proving it needs a stripped copy of the whole
            # file, so the caller that materialises that copy decides — see may_attempt.
            return KeyValidationResult(
                "unverified",
                None,
                "QQNT custom database passed format, runtime and key-shape checks; readability "
                "is confirmed only when the stripped copy actually opens",
            )
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
