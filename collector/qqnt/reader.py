from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any


class ReadOnlyDatabaseError(RuntimeError):
    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


_DENIED_ACTIONS = frozenset(
    action
    for action in (
        getattr(sqlite3, "SQLITE_INSERT", None),
        getattr(sqlite3, "SQLITE_UPDATE", None),
        getattr(sqlite3, "SQLITE_DELETE", None),
        getattr(sqlite3, "SQLITE_CREATE_INDEX", None),
        getattr(sqlite3, "SQLITE_CREATE_TABLE", None),
        getattr(sqlite3, "SQLITE_CREATE_TEMP_INDEX", None),
        getattr(sqlite3, "SQLITE_CREATE_TEMP_TABLE", None),
        getattr(sqlite3, "SQLITE_CREATE_TEMP_TRIGGER", None),
        getattr(sqlite3, "SQLITE_CREATE_TEMP_VIEW", None),
        getattr(sqlite3, "SQLITE_CREATE_TRIGGER", None),
        getattr(sqlite3, "SQLITE_CREATE_VIEW", None),
        getattr(sqlite3, "SQLITE_DROP_INDEX", None),
        getattr(sqlite3, "SQLITE_DROP_TABLE", None),
        getattr(sqlite3, "SQLITE_DROP_TEMP_INDEX", None),
        getattr(sqlite3, "SQLITE_DROP_TEMP_TABLE", None),
        getattr(sqlite3, "SQLITE_DROP_TEMP_TRIGGER", None),
        getattr(sqlite3, "SQLITE_DROP_TEMP_VIEW", None),
        getattr(sqlite3, "SQLITE_DROP_TRIGGER", None),
        getattr(sqlite3, "SQLITE_DROP_VIEW", None),
        getattr(sqlite3, "SQLITE_ALTER_TABLE", None),
        getattr(sqlite3, "SQLITE_REINDEX", None),
        getattr(sqlite3, "SQLITE_ANALYZE", None),
        getattr(sqlite3, "SQLITE_ATTACH", None),
        getattr(sqlite3, "SQLITE_DETACH", None),
    )
    if action is not None
)


def _authorizer(action: int, _arg1: str | None, _arg2: str | None, _database: str | None, _trigger: str | None) -> int:
    return sqlite3.SQLITE_DENY if action in _DENIED_ACTIONS else sqlite3.SQLITE_OK


class ReadOnlySQLiteDatabase:
    def __init__(
        self,
        path: str | Path,
        *,
        immutable: bool = False,
        key: str | None = None,
        cipher: bool = False,
    ) -> None:
        self.path = Path(path).expanduser().resolve()
        self.immutable = immutable
        self.key = key
        self.cipher = cipher

    def connect(self) -> sqlite3.Connection:
        if not self.path.is_file():
            raise ReadOnlyDatabaseError("DB_OPEN_FAILED", "database file does not exist")
        dbapi = sqlite3
        if self.cipher:
            from collector.qqnt.sqlcipher import configure_connection, load_sqlcipher

            dbapi = load_sqlcipher()
        connection: sqlite3.Connection | None = None
        try:
            if self.cipher:
                connection = dbapi.connect(str(self.path), isolation_level=None)
                connection.row_factory = dbapi.Row
                configure_connection(connection, self.key or "")
            else:
                query = "mode=ro"
                if self.immutable:
                    query += "&immutable=1"
                uri = f"file:{self.path.as_posix()}?{query}"
                connection = sqlite3.connect(uri, uri=True, timeout=10, isolation_level=None)
                connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            with contextlib.suppress(Exception):
                connection.execute("PRAGMA trusted_schema=OFF")
            connection.execute("PRAGMA busy_timeout=10000")
            connection.set_authorizer(_authorizer)
            connection.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
            return connection
        except Exception as exc:
            if connection is not None:
                connection.close()
            error_code = "DB_KEY_INVALID" if self.cipher else "DB_OPEN_FAILED"
            raise ReadOnlyDatabaseError(error_code, "database could not be opened read-only") from exc

    @contextlib.contextmanager
    def read_transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN")
            yield connection
            connection.execute("COMMIT")
        except Exception:
            with contextlib.suppress(sqlite3.Error):
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def query(self, sql: str, parameters: Sequence[Any] = ()) -> list[sqlite3.Row]:
        normalized = sql.lstrip().upper()
        if not normalized.startswith(("SELECT", "WITH", "PRAGMA", "EXPLAIN")):
            raise ReadOnlyDatabaseError("DB_OPEN_FAILED", "only read-only SQL is allowed")
        try:
            with self.read_transaction() as connection:
                return list(connection.execute(sql, tuple(parameters)).fetchall())
        except sqlite3.Error as exc:
            raise ReadOnlyDatabaseError("DB_READ_INCONSISTENT", "read-only query failed") from exc


class QQNTReader:
    def __init__(self, database: ReadOnlySQLiteDatabase) -> None:
        self.database = database

    def probe(self):
        from collector.qqnt.schema_probe import probe_schema

        return probe_schema(self.database)

    def adapter(self):
        from collector.qqnt.versions import select_adapter

        return select_adapter(self.probe())

    def read_pages(
        self,
        *,
        cursor_by_table: dict[str, Any] | None = None,
        page_size: int = 200,
        overlap_seconds: int = 0,
    ):
        from collector.qqnt.versions import select_adapter
        from collector.sync.cursor import IncrementalCursor

        report = self.probe()
        adapter = select_adapter(report)
        cursors = cursor_by_table or {}
        for candidate in adapter.candidates(report):
            cursor_value = cursors.get(candidate.table)
            cursor = (
                IncrementalCursor.from_dict(cursor_value)
                if isinstance(cursor_value, dict)
                else cursor_value or IncrementalCursor()
            )
            yield candidate, adapter.read_page(
                self.database,
                candidate,
                cursor,
                limit=page_size,
                overlap_seconds=overlap_seconds,
            )
