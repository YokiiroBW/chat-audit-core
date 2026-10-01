from __future__ import annotations

import contextlib
import json
import sqlite3
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any


class CollectorStateError(RuntimeError):
    pass


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS state_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL,
    account_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    server_source_id TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE(source_type, account_id, device_id)
);

CREATE TABLE IF NOT EXISTS table_cursors (
    source_id TEXT NOT NULL,
    table_name TEXT NOT NULL,
    cursor_json TEXT NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY(source_id, table_name),
    FOREIGN KEY(source_id) REFERENCES sources(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS conversation_cursors (
    source_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    cursor_json TEXT NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY(source_id, conversation_id),
    FOREIGN KEY(source_id) REFERENCES sources(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS pending_messages (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    dedupe_key TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at INTEGER NOT NULL DEFAULT 0,
    lease_until INTEGER,
    last_error_code TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    FOREIGN KEY(source_id) REFERENCES sources(id) ON DELETE CASCADE,
    CHECK(status IN ('pending','inflight','retry','dead_letter','completed')),
    CHECK(attempts >= 0)
);

CREATE INDEX IF NOT EXISTS idx_pending_messages_ready
ON pending_messages(status, next_attempt_at, created_at);

CREATE TABLE IF NOT EXISTS pending_media (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    message_queue_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    media_role TEXT NOT NULL DEFAULT 'asset',
    media_type TEXT NOT NULL,
    file_name TEXT,
    staging_path TEXT NOT NULL,
    file_hash TEXT NOT NULL,
    file_size INTEGER NOT NULL,
    content_sha256 TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at INTEGER NOT NULL DEFAULT 0,
    lease_until INTEGER,
    last_error_code TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE(message_queue_id, ordinal, media_role),
    FOREIGN KEY(source_id) REFERENCES sources(id) ON DELETE CASCADE,
    FOREIGN KEY(message_queue_id) REFERENCES pending_messages(id) ON DELETE CASCADE,
    CHECK(status IN ('pending','inflight','retry','dead_letter','completed')),
    CHECK(attempts >= 0),
    CHECK(ordinal >= 0),
    CHECK(media_role IN ('asset','thumbnail','artifact')),
    CHECK(file_size > 0)
);

CREATE INDEX IF NOT EXISTS idx_pending_media_ready
ON pending_media(status, next_attempt_at, created_at);

CREATE TABLE IF NOT EXISTS completed_uploads (
    queue_kind TEXT NOT NULL,
    queue_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    completed_at INTEGER NOT NULL,
    PRIMARY KEY(queue_kind, queue_id)
);

CREATE TABLE IF NOT EXISTS sync_runs (
    id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at INTEGER NOT NULL,
    completed_at INTEGER,
    stats_json TEXT NOT NULL DEFAULT '{}',
    error_code TEXT,
    detail TEXT,
    CHECK(status IN ('running','completed','partial','failed'))
);

CREATE INDEX IF NOT EXISTS idx_sync_runs_started
ON sync_runs(started_at DESC);

CREATE TABLE IF NOT EXISTS parser_failures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL,
    source_table TEXT NOT NULL,
    source_key_hash TEXT NOT NULL,
    error_code TEXT NOT NULL,
    detail TEXT,
    created_at INTEGER NOT NULL,
    resolved_at INTEGER,
    FOREIGN KEY(source_id) REFERENCES sources(id) ON DELETE CASCADE
);
"""


class CollectorStateStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self._active_connection: sqlite3.Connection | None = None

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA_SQL)
            media_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(pending_media)").fetchall()
            }
            media_table_row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='pending_media'"
            ).fetchone()
            media_table_sql = str(media_table_row["sql"] or "") if media_table_row else ""
            if "media_role" not in media_columns or "'artifact'" not in media_table_sql:
                had_media_role = "media_role" in media_columns
                connection.execute("ALTER TABLE pending_media RENAME TO pending_media_v1")
                connection.execute(
                    """
                    CREATE TABLE pending_media (
                        id TEXT PRIMARY KEY,
                        source_id TEXT NOT NULL,
                        message_queue_id TEXT NOT NULL,
                        ordinal INTEGER NOT NULL,
                        media_role TEXT NOT NULL DEFAULT 'asset',
                        media_type TEXT NOT NULL,
                        file_name TEXT,
                        staging_path TEXT NOT NULL,
                        file_hash TEXT NOT NULL,
                        file_size INTEGER NOT NULL,
                        content_sha256 TEXT NOT NULL,
                        payload_json TEXT NOT NULL DEFAULT '{}',
                        status TEXT NOT NULL DEFAULT 'pending',
                        attempts INTEGER NOT NULL DEFAULT 0,
                        next_attempt_at INTEGER NOT NULL DEFAULT 0,
                        lease_until INTEGER,
                        last_error_code TEXT,
                        created_at INTEGER NOT NULL,
                        updated_at INTEGER NOT NULL,
                        UNIQUE(message_queue_id, ordinal, media_role),
                        FOREIGN KEY(source_id) REFERENCES sources(id) ON DELETE CASCADE,
                        FOREIGN KEY(message_queue_id) REFERENCES pending_messages(id) ON DELETE CASCADE,
                        CHECK(status IN ('pending','inflight','retry','dead_letter','completed')),
                        CHECK(attempts >= 0),
                        CHECK(ordinal >= 0),
                        CHECK(media_role IN ('asset','thumbnail','artifact')),
                        CHECK(file_size > 0)
                    )
                    """
                )
                media_role_expression = "media_role" if had_media_role else "'asset'"
                connection.execute(
                    f"""
                    INSERT INTO pending_media(
                        id,source_id,message_queue_id,ordinal,media_role,media_type,file_name,
                        staging_path,file_hash,file_size,content_sha256,payload_json,status,
                        attempts,next_attempt_at,lease_until,last_error_code,created_at,updated_at
                    )
                    SELECT
                        id,source_id,message_queue_id,ordinal,{media_role_expression},media_type,file_name,
                        staging_path,file_hash,file_size,content_sha256,payload_json,status,
                        attempts,next_attempt_at,lease_until,last_error_code,created_at,updated_at
                    FROM pending_media_v1
                    """
                )
                connection.execute("DROP TABLE pending_media_v1")
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_pending_media_ready "
                    "ON pending_media(status, next_attempt_at, created_at)"
                )
            connection.execute(
                "INSERT INTO state_meta(key, value) VALUES('schema_version', '3') "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
            )

    def get_meta(self, key: str) -> str | None:
        with self.connect() as connection:
            row = connection.execute("SELECT value FROM state_meta WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self.connect(immediate=True) as connection:
            connection.execute(
                "INSERT INTO state_meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def _open(self, *, immediate: bool) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        return connection

    @contextlib.contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        """Run a batch of queue writes as one connection and one commit.

        Without this, every enqueued message opened its own connection, set
        three pragmas, began a transaction and committed -- and under WAL each
        of those commits is an fsync. Scanning a database meant one fsync per
        message, which is what made a large initial import disk-bound rather
        than parse-bound.

        Reentrant: a nested scope joins the open transaction instead of
        deadlocking against its own write lock.
        """
        if self._active_connection is not None:
            yield self._active_connection
            return
        connection = self._open(immediate=immediate)
        self._active_connection = connection
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            self._active_connection = None
            connection.close()

    @contextlib.contextmanager
    def connect(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        # Inside a transaction() scope every write joins that one transaction,
        # so callers need no changes to benefit from the batching.
        if self._active_connection is not None:
            yield self._active_connection
            return
        connection = self._open(immediate=immediate)
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def upsert_source(
        self,
        *,
        source_id: str,
        source_type: str,
        account_id: str,
        device_id: str,
        server_source_id: str | None = None,
        status: str = "active",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        now = int(time.time())
        with self.connect(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO sources(
                    id, source_type, account_id, device_id, server_source_id,
                    status, metadata_json, created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    source_type=excluded.source_type,
                    account_id=excluded.account_id,
                    device_id=excluded.device_id,
                    server_source_id=COALESCE(excluded.server_source_id, sources.server_source_id),
                    status=excluded.status,
                    metadata_json=excluded.metadata_json,
                    updated_at=excluded.updated_at
                """,
                (
                    source_id,
                    source_type,
                    account_id,
                    device_id,
                    server_source_id,
                    status,
                    json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
                    now,
                    now,
                ),
            )

    def update_server_source_id(self, source_id: str, server_source_id: str) -> None:
        with self.connect(immediate=True) as connection:
            cursor = connection.execute(
                "UPDATE sources SET server_source_id=?, updated_at=? WHERE id=?",
                (server_source_id, int(time.time()), source_id),
            )
            if cursor.rowcount != 1:
                raise CollectorStateError(f"unknown collector source: {source_id}")

    def get_source(self, source_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        return dict(row) if row else None

    def set_cursor(self, *, source_id: str, scope: str, name: str, value: dict[str, Any]) -> None:
        if scope not in {"table", "conversation"}:
            raise CollectorStateError("cursor scope must be table or conversation")
        table = "table_cursors" if scope == "table" else "conversation_cursors"
        key = "table_name" if scope == "table" else "conversation_id"
        with self.connect(immediate=True) as connection:
            connection.execute(
                f"INSERT INTO {table}(source_id,{key},cursor_json,updated_at) VALUES(?,?,?,?) "
                f"ON CONFLICT(source_id,{key}) DO UPDATE SET cursor_json=excluded.cursor_json, updated_at=excluded.updated_at",
                (source_id, name, json.dumps(value, ensure_ascii=False, sort_keys=True), int(time.time())),
            )

    def get_cursor(self, *, source_id: str, scope: str, name: str) -> dict[str, Any] | None:
        if scope not in {"table", "conversation"}:
            raise CollectorStateError("cursor scope must be table or conversation")
        table = "table_cursors" if scope == "table" else "conversation_cursors"
        key = "table_name" if scope == "table" else "conversation_id"
        with self.connect() as connection:
            row = connection.execute(
                f"SELECT cursor_json FROM {table} WHERE source_id=? AND {key}=?",
                (source_id, name),
            ).fetchone()
        return json.loads(row["cursor_json"]) if row else None

    def try_start_sync_run(self, mode: str) -> str | None:
        run_id = uuid.uuid4().hex
        with self.connect(immediate=True) as connection:
            active = connection.execute(
                "SELECT id FROM sync_runs WHERE status='running' LIMIT 1"
            ).fetchone()
            if active is not None:
                return None
            connection.execute(
                "INSERT INTO sync_runs(id,mode,status,started_at) VALUES(?,?,?,?)",
                (run_id, mode, "running", int(time.time())),
            )
        return run_id

    def recover_interrupted_runs(self, *, detail: str = "collector process restarted") -> int:
        now = int(time.time())
        with self.connect(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE sync_runs
                SET status='failed', completed_at=?, error_code='COLLECTOR_RESTARTED', detail=?
                WHERE status='running'
                """,
                (now, detail),
            )
        return int(cursor.rowcount)

    def start_sync_run(self, mode: str) -> str:
        run_id = self.try_start_sync_run(mode)
        if run_id is None:
            raise CollectorStateError("SYNC_ALREADY_RUNNING")
        return run_id

    def finish_sync_run(
        self,
        run_id: str,
        *,
        status: str,
        stats: dict[str, Any] | None = None,
        error_code: str | None = None,
        detail: str | None = None,
    ) -> None:
        if status not in {"completed", "partial", "failed"}:
            raise CollectorStateError("invalid terminal sync status")
        with self.connect(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE sync_runs
                SET status=?, completed_at=?, stats_json=?, error_code=?, detail=?
                WHERE id=? AND status='running'
                """,
                (
                    status,
                    int(time.time()),
                    json.dumps(stats or {}, ensure_ascii=False, sort_keys=True),
                    error_code,
                    detail,
                    run_id,
                ),
            )
            if cursor.rowcount != 1:
                raise CollectorStateError(f"sync run is not active: {run_id}")

    def record_parser_failure(
        self,
        *,
        source_id: str,
        source_table: str,
        source_key_hash: str,
        error_code: str,
        detail: str | None = None,
    ) -> None:
        with self.connect(immediate=True) as connection:
            existing = connection.execute(
                """
                SELECT id FROM parser_failures
                WHERE source_id=? AND source_table=? AND source_key_hash=?
                  AND error_code=? AND resolved_at IS NULL
                LIMIT 1
                """,
                (source_id, source_table, source_key_hash, error_code),
            ).fetchone()
            if existing is not None:
                return
            connection.execute(
                """
                INSERT INTO parser_failures(
                    source_id, source_table, source_key_hash, error_code, detail, created_at
                ) VALUES(?,?,?,?,?,?)
                """,
                (source_id, source_table, source_key_hash, error_code, detail, int(time.time())),
            )

    def resolve_parser_failures(
        self,
        *,
        source_id: str,
        source_table: str,
        source_key_hash: str,
        error_codes: tuple[str, ...] = ("PROTOBUF_PARSE_FAILED", "MESSAGE_PARSE_FAILED"),
    ) -> None:
        if not error_codes:
            return
        placeholders = ",".join("?" for _ in error_codes)
        with self.connect(immediate=True) as connection:
            connection.execute(
                f"""
                UPDATE parser_failures
                SET resolved_at=?
                WHERE source_id=? AND source_table=? AND source_key_hash=?
                  AND resolved_at IS NULL AND error_code IN ({placeholders})
                """,
                (int(time.time()), source_id, source_table, source_key_hash, *error_codes),
            )

    def status_snapshot(self) -> dict[str, Any]:
        with self.connect() as connection:
            queue_counts: dict[str, dict[str, int]] = {}
            for kind, table in (("messages", "pending_messages"), ("media", "pending_media")):
                rows = connection.execute(
                    f"SELECT status, COUNT(*) AS count FROM {table} GROUP BY status"
                ).fetchall()
                queue_counts[kind] = {str(row["status"]): int(row["count"]) for row in rows}
            last_run = connection.execute(
                "SELECT id,mode,status,started_at,completed_at,error_code,stats_json "
                "FROM sync_runs ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            parser_failures = connection.execute(
                "SELECT COUNT(*) AS count FROM parser_failures WHERE resolved_at IS NULL"
            ).fetchone()
        last_run_value = dict(last_run) if last_run else None
        if last_run_value:
            last_run_value["stats"] = json.loads(last_run_value.pop("stats_json"))
        return {
            "queues": queue_counts,
            "unresolved_parser_failures": int(parser_failures["count"]),
            "last_run": last_run_value,
        }
