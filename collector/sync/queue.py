from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Literal

from collector.sync.state import CollectorStateError, CollectorStateStore


QueueKind = Literal["message", "media"]
RETRY_DELAYS_SECONDS = (10, 30, 120, 600, 1800)


@dataclass(frozen=True)
class QueueItem:
    kind: QueueKind
    id: str
    source_id: str
    payload: dict[str, Any]
    attempts: int
    message_queue_id: str | None = None
    ordinal: int | None = None
    media_role: str | None = None
    media_type: str | None = None
    file_name: str | None = None
    staging_path: str | None = None
    file_hash: str | None = None
    file_size: int | None = None
    content_sha256: str | None = None


class CollectorQueue:
    def __init__(self, store: CollectorStateStore) -> None:
        self.store = store

    @staticmethod
    def _stable_id(prefix: str, value: str) -> str:
        return f"{prefix}-{hashlib.sha256(value.encode('utf-8')).hexdigest()}"

    def enqueue_message(
        self,
        *,
        source_id: str,
        dedupe_key: str,
        payload: dict[str, Any],
        force_refresh: bool = False,
    ) -> str:
        queue_id = self._stable_id("msg", f"{source_id}:{dedupe_key}")
        now = int(time.time())
        with self.store.connect(immediate=True) as connection:
            if force_refresh:
                update_sql = """
                    payload_json=excluded.payload_json,
                    status='pending',
                    next_attempt_at=0,
                    attempts=0,
                    updated_at=excluded.updated_at
                """
            else:
                update_sql = """
                    payload_json=CASE
                        WHEN pending_messages.status='completed' THEN pending_messages.payload_json
                        ELSE excluded.payload_json
                    END,
                    updated_at=excluded.updated_at
                """
            connection.execute(
                f"""
                INSERT INTO pending_messages(
                    id,source_id,dedupe_key,payload_json,status,next_attempt_at,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(dedupe_key) DO UPDATE SET
                    {update_sql}
                """,
                (
                    queue_id,
                    source_id,
                    dedupe_key,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    "pending",
                    0,
                    now,
                    now,
                ),
            )
        return queue_id

    def enqueue_media(
        self,
        *,
        source_id: str,
        message_queue_id: str,
        ordinal: int,
        media_role: str = "asset",
        media_type: str,
        staging_path: str,
        file_hash: str,
        file_size: int,
        content_sha256: str,
        file_name: str | None = None,
        payload: dict[str, Any] | None = None,
        force_refresh: bool = False,
    ) -> str:
        if media_role not in {"asset", "thumbnail", "artifact"}:
            raise CollectorStateError("media_role must be asset, thumbnail, or artifact")
        queue_id = self._stable_id("media", f"{message_queue_id}:{ordinal}:{media_role}")
        now = int(time.time())
        with self.store.connect(immediate=True) as connection:
            existing = connection.execute(
                "SELECT id,status FROM pending_media WHERE message_queue_id=? AND ordinal=? AND media_role=?",
                (message_queue_id, ordinal, media_role),
            ).fetchone()
            if existing is not None and existing["status"] == "completed" and not force_refresh:
                return str(existing["id"])
            connection.execute(
                """
                INSERT INTO pending_media(
                    id,source_id,message_queue_id,ordinal,media_role,media_type,file_name,staging_path,
                    file_hash,file_size,content_sha256,payload_json,status,next_attempt_at,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(message_queue_id,ordinal,media_role) DO UPDATE SET
                    media_type=excluded.media_type,
                    file_name=excluded.file_name,
                    staging_path=excluded.staging_path,
                    file_hash=excluded.file_hash,
                    file_size=excluded.file_size,
                    content_sha256=excluded.content_sha256,
                    payload_json=excluded.payload_json,
                    status=CASE WHEN pending_media.status='completed' AND NOT ? THEN 'completed' ELSE 'pending' END,
                    next_attempt_at=CASE WHEN pending_media.status='completed' AND NOT ? THEN pending_media.next_attempt_at ELSE 0 END,
                    updated_at=excluded.updated_at
                """,
                (
                    queue_id,
                    source_id,
                    message_queue_id,
                    ordinal,
                    media_role,
                    media_type,
                    file_name,
                    staging_path,
                    file_hash,
                    file_size,
                    content_sha256,
                    json.dumps(payload or {}, ensure_ascii=False, sort_keys=True),
                    "pending",
                    0,
                    now,
                    now,
                    force_refresh,
                    force_refresh,
                ),
            )
        return queue_id

    @staticmethod
    def _table(kind: QueueKind) -> str:
        if kind == "message":
            return "pending_messages"
        if kind == "media":
            return "pending_media"
        raise CollectorStateError(f"unsupported queue kind: {kind}")

    def lease_ready(
        self,
        kind: QueueKind,
        *,
        limit: int,
        lease_seconds: int = 120,
    ) -> list[QueueItem]:
        if limit <= 0:
            return []
        table = self._table(kind)
        now = int(time.time())
        with self.store.connect(immediate=True) as connection:
            connection.execute(
                f"UPDATE {table} SET status='retry', lease_until=NULL, updated_at=? "
                "WHERE status='inflight' AND lease_until IS NOT NULL AND lease_until<=?",
                (now, now),
            )
            readiness = ""
            if kind == "message":
                # A message waits for its media, but only for media that can still
                # make progress. Media that reached dead_letter never becomes
                # 'completed', so gating on 'completed' alone parks the message in
                # 'pending' for ever: it is never uploaded, never counted as a dead
                # letter, and requeue_dead_letter cannot reach it because the
                # message row itself is not dead. The message reference already has
                # an archive_state of 'failed' to describe the missing media, so let
                # it ship with that.
                readiness = (
                    "AND NOT EXISTS (SELECT 1 FROM pending_media media "
                    "WHERE media.message_queue_id=pending_messages.id "
                    "AND media.status NOT IN ('completed','dead_letter'))"
                )
            rows = connection.execute(
                f"SELECT * FROM {table} WHERE status IN ('pending','retry') "
                f"AND next_attempt_at<=? {readiness} ORDER BY created_at,id LIMIT ?",
                (now, limit),
            ).fetchall()
            ids = [str(row["id"]) for row in rows]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                connection.execute(
                    f"UPDATE {table} SET status='inflight', attempts=attempts+1, lease_until=?, updated_at=? "
                    f"WHERE id IN ({placeholders})",
                    (now + lease_seconds, now, *ids),
                )
                rows = connection.execute(
                    f"SELECT * FROM {table} WHERE id IN ({placeholders}) ORDER BY created_at,id",
                    ids,
                ).fetchall()

        items: list[QueueItem] = []
        for row in rows:
            values = dict(row)
            items.append(
                QueueItem(
                    kind=kind,
                    id=str(values["id"]),
                    source_id=str(values["source_id"]),
                    payload=json.loads(values["payload_json"]),
                    attempts=int(values["attempts"]),
                    message_queue_id=values.get("message_queue_id"),
                    ordinal=values.get("ordinal"),
                    media_role=values.get("media_role"),
                    media_type=values.get("media_type"),
                    file_name=values.get("file_name"),
                    staging_path=values.get("staging_path"),
                    file_hash=values.get("file_hash"),
                    file_size=values.get("file_size"),
                    content_sha256=values.get("content_sha256"),
                )
            )
        return items

    def mark_completed(self, item: QueueItem, response: dict[str, Any]) -> None:
        table = self._table(item.kind)
        now = int(time.time())
        with self.store.connect(immediate=True) as connection:
            cursor = connection.execute(
                f"UPDATE {table} SET status='completed', lease_until=NULL, last_error_code=NULL, updated_at=? "
                "WHERE id=? AND status='inflight'",
                (now, item.id),
            )
            if cursor.rowcount != 1:
                raise CollectorStateError(f"queue item is not leased: {item.id}")
            connection.execute(
                "INSERT INTO completed_uploads(queue_kind,queue_id,response_json,completed_at) VALUES(?,?,?,?) "
                "ON CONFLICT(queue_kind,queue_id) DO UPDATE SET response_json=excluded.response_json, completed_at=excluded.completed_at",
                (item.kind, item.id, json.dumps(response, ensure_ascii=False, sort_keys=True), now),
            )

    def mark_retry(self, item: QueueItem, error_code: str, *, max_attempts: int = 8) -> str:
        table = self._table(item.kind)
        terminal = item.attempts >= max_attempts
        status = "dead_letter" if terminal else "retry"
        delay_index = min(max(item.attempts - 1, 0), len(RETRY_DELAYS_SECONDS) - 1)
        next_attempt = int(time.time()) + (0 if terminal else RETRY_DELAYS_SECONDS[delay_index])
        with self.store.connect(immediate=True) as connection:
            cursor = connection.execute(
                f"UPDATE {table} SET status=?, next_attempt_at=?, lease_until=NULL, last_error_code=?, updated_at=? "
                "WHERE id=? AND status='inflight'",
                (status, next_attempt, error_code, int(time.time()), item.id),
            )
            if cursor.rowcount != 1:
                raise CollectorStateError(f"queue item is not leased: {item.id}")
        return status

    def requeue_dead_letter(self, kind: QueueKind, queue_id: str) -> bool:
        table = self._table(kind)
        with self.store.connect(immediate=True) as connection:
            cursor = connection.execute(
                f"UPDATE {table} SET status='pending', attempts=0, next_attempt_at=0, last_error_code=NULL, updated_at=? "
                "WHERE id=? AND status='dead_letter'",
                (int(time.time()), queue_id),
            )
        return cursor.rowcount == 1

    def patch_message_media(
        self,
        message_queue_id: str,
        ordinal: int,
        uploaded: dict[str, Any],
        *,
        media_role: str = "asset",
    ) -> None:
        with self.store.connect(immediate=True) as connection:
            row = connection.execute(
                "SELECT payload_json,status FROM pending_messages WHERE id=?",
                (message_queue_id,),
            ).fetchone()
            if row is None:
                raise CollectorStateError(f"unknown message queue item: {message_queue_id}")
            if row["status"] == "completed":
                return
            payload = json.loads(row["payload_json"])
            if media_role in {"asset", "thumbnail"}:
                references = payload.get("media") or []
                reference = next((entry for entry in references if entry.get("ordinal") == ordinal), None)
                if reference is None:
                    raise CollectorStateError(f"message media ordinal not found: {message_queue_id}:{ordinal}")
            if media_role == "asset":
                reference.update(
                    {
                        "source_state": "downloaded",
                        "archive_state": "complete",
                        "asset_file_hash": uploaded["file_hash"],
                        "actual_file_size": uploaded["file_size"],
                        "failure_code": None,
                        "failure_detail": None,
                    }
                )
            elif media_role == "thumbnail":
                reference.update(
                    {
                        "archive_state": "thumbnail_only",
                        "thumbnail_file_hash": uploaded["file_hash"],
                    }
                )
            elif media_role == "artifact":
                replace_token = uploaded.get("replace_token")
                replacement = uploaded.get("replacement")
                if not isinstance(replace_token, str) or not replace_token:
                    raise CollectorStateError("artifact upload is missing replace_token")
                if not isinstance(replacement, str) or not replacement:
                    raise CollectorStateError("artifact upload is missing replacement")
                for field in ("raw_message", "local_message"):
                    value = payload.get("message", {}).get(field)
                    if isinstance(value, str):
                        payload["message"][field] = value.replace(replace_token, replacement)
            else:
                raise CollectorStateError("media_role must be asset, thumbnail, or artifact")
            now = int(time.time())
            if row["status"] == "dead_letter":
                connection.execute(
                    """
                    UPDATE pending_messages
                    SET payload_json=?, status='pending', attempts=0, next_attempt_at=0,
                        lease_until=NULL, last_error_code=NULL, updated_at=?
                    WHERE id=?
                    """,
                    (json.dumps(payload, ensure_ascii=False, sort_keys=True), now, message_queue_id),
                )
            else:
                connection.execute(
                    "UPDATE pending_messages SET payload_json=?, updated_at=? WHERE id=?",
                    (json.dumps(payload, ensure_ascii=False, sort_keys=True), now, message_queue_id),
                )

    def counts(self) -> dict[str, dict[str, int]]:
        return self.store.status_snapshot()["queues"]
