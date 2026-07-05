from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
import time
from typing import Any


@dataclass(frozen=True)
class PendingItem:
    id: int
    kind: str
    payload: dict[str, Any]
    attempts: int
    last_error: str | None
    created_at: int


class PendingEventQueue:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def enqueue(self, kind: str, payload: dict[str, Any]) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO pending_items(kind, payload_json, attempts, created_at) VALUES (?, ?, 0, ?)",
                (kind, json.dumps(payload, ensure_ascii=False, separators=(",", ":")), int(time.time())),
            )
            return int(cursor.lastrowid)

    def list_pending(self, limit: int = 100) -> list[PendingItem]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, kind, payload_json, attempts, last_error, created_at
                FROM pending_items
                ORDER BY id ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            PendingItem(
                id=int(row["id"]),
                kind=str(row["kind"]),
                payload=json.loads(row["payload_json"]),
                attempts=int(row["attempts"]),
                last_error=row["last_error"],
                created_at=int(row["created_at"]),
            )
            for row in rows
        ]

    def mark_done(self, item_id: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM pending_items WHERE id = ?", (item_id,))

    def mark_failed(self, item_id: int, error: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE pending_items SET attempts = attempts + 1, last_error = ? WHERE id = ?",
                (error[:1000], item_id),
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pending_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at INTEGER NOT NULL
                )
                """
            )
