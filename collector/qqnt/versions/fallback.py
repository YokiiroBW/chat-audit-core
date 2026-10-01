from __future__ import annotations

from typing import Any

from collector.qqnt.reader import ReadOnlyDatabaseError, ReadOnlySQLiteDatabase
from collector.qqnt.schema_probe import MessageTableCandidate, SchemaProbeReport
from collector.qqnt.versions.base import MessagePage, SchemaAdapterError, SourceMessageRow
from collector.sync.cursor import IncrementalCursor


def _quote(identifier: str) -> str:
    if not identifier or "\x00" in identifier:
        raise SchemaAdapterError("DB_SCHEMA_UNSUPPORTED", "invalid SQLite identifier")
    return '"' + identifier.replace('"', '""') + '"'


def _integer(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


class FallbackSQLiteAdapter:
    name = "fallback-sqlite"

    def __init__(self) -> None:
        self._cursor_type_cache: dict[tuple[str, str], tuple[bool, bool]] = {}

    def supports(self, report: SchemaProbeReport) -> bool:
        return bool(report.message_candidates)

    def candidates(self, report: SchemaProbeReport) -> tuple[MessageTableCandidate, ...]:
        return report.message_candidates

    @staticmethod
    def _identity_expression(candidate: MessageTableCandidate, without_rowid: bool) -> str:
        if not without_rowid:
            return "rowid"
        return _quote(candidate.mapping["msg_id"])

    def _cursor_types(
        self,
        database: ReadOnlySQLiteDatabase,
        candidate: MessageTableCandidate,
    ) -> tuple[bool, bool]:
        cache_key = (str(database.path), candidate.table)
        cached = self._cursor_type_cache.get(cache_key)
        if cached is not None:
            return cached
        table_info = database.query(f"PRAGMA table_info({_quote(candidate.table)})")
        table_sql_rows = database.query(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (candidate.table,),
        )
        without_rowid = bool(table_sql_rows and "WITHOUT ROWID" in str(table_sql_rows[0]["sql"] or "").upper())
        declared_types = {str(row["name"]): str(row["type"] or "").upper() for row in table_info}
        id_is_integer = "INT" in declared_types.get(candidate.mapping["msg_id"], "")
        value = (id_is_integer, not without_rowid or id_is_integer)
        self._cursor_type_cache[cache_key] = value
        return value

    @staticmethod
    def _ordered_component(value: str, integer: bool) -> int | str:
        return _integer(value) if integer else value

    def cursor_after(
        self,
        database: ReadOnlySQLiteDatabase,
        candidate: MessageTableCandidate,
        value: IncrementalCursor,
        baseline: IncrementalCursor,
    ) -> bool:
        id_is_integer, identity_is_integer = self._cursor_types(database, candidate)
        value_key = (
            value.last_msg_time,
            value.last_msg_seq,
            self._ordered_component(value.last_msg_id, id_is_integer),
            self._ordered_component(value.last_row_identity, identity_is_integer),
        )
        baseline_key = (
            baseline.last_msg_time,
            baseline.last_msg_seq,
            self._ordered_component(baseline.last_msg_id, id_is_integer),
            self._ordered_component(baseline.last_row_identity, identity_is_integer),
        )
        return value_key > baseline_key

    def read_page(
        self,
        database: ReadOnlySQLiteDatabase,
        candidate: MessageTableCandidate,
        cursor: IncrementalCursor,
        *,
        limit: int,
        overlap_seconds: int = 0,
    ) -> MessagePage:
        if not 1 <= limit <= 1000:
            raise ValueError("message page limit must be between 1 and 1000")
        table_info = database.query(f"PRAGMA table_info({_quote(candidate.table)})")
        if not table_info:
            raise SchemaAdapterError("DB_SCHEMA_UNSUPPORTED", f"message table disappeared: {candidate.table}")
        table_sql_rows = database.query(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (candidate.table,),
        )
        without_rowid = bool(table_sql_rows and "WITHOUT ROWID" in str(table_sql_rows[0]["sql"] or "").upper())

        mapping = candidate.mapping
        declared_types = {str(row["name"]): str(row["type"] or "").upper() for row in table_info}
        time_expression = _quote(mapping["msg_time"])
        seq_expression = _quote(mapping["msg_seq"]) if "msg_seq" in mapping else "0"
        id_expression = _quote(mapping["msg_id"])
        identity_expression = self._identity_expression(candidate, without_rowid)
        id_is_integer = "INT" in declared_types.get(mapping["msg_id"], "")
        id_order_expression = f"CAST({id_expression} AS INTEGER)" if id_is_integer else f"CAST({id_expression} AS TEXT)"
        identity_is_integer = not without_rowid or id_is_integer
        identity_order_expression = (
            f"CAST({identity_expression} AS INTEGER)"
            if identity_is_integer
            else f"CAST({identity_expression} AS TEXT)"
        )
        order_sql = (
            f"{time_expression} ASC, {seq_expression} ASC, "
            f"{id_order_expression} ASC, {identity_order_expression} ASC"
        )

        parameters: list[Any]
        if overlap_seconds > 0 and cursor.last_msg_time > 0:
            where_sql = f"WHERE {time_expression}>=?"
            parameters = [cursor.overlap_start(overlap_seconds)]
        elif cursor.last_msg_time > 0 or cursor.last_msg_id or cursor.last_row_identity:
            where_sql = f"""
            WHERE
                {time_expression}>?
                OR ({time_expression}=? AND {seq_expression}>?)
                OR ({time_expression}=? AND {seq_expression}=? AND {id_order_expression}>?)
                OR (
                    {time_expression}=? AND {seq_expression}=?
                    AND {id_order_expression}=? AND {identity_order_expression}>?
                )
            """
            parameters = [
                cursor.last_msg_time,
                cursor.last_msg_time,
                cursor.last_msg_seq,
                cursor.last_msg_time,
                cursor.last_msg_seq,
                _integer(cursor.last_msg_id) if id_is_integer else cursor.last_msg_id,
                cursor.last_msg_time,
                cursor.last_msg_seq,
                _integer(cursor.last_msg_id) if id_is_integer else cursor.last_msg_id,
                _integer(cursor.last_row_identity) if identity_is_integer else cursor.last_row_identity,
            ]
        else:
            where_sql = ""
            parameters = []

        sql = (
            f"SELECT {identity_expression} AS __row_identity, * FROM {_quote(candidate.table)} "
            f"{where_sql} ORDER BY {order_sql} LIMIT ?"
        )
        parameters.append(limit)
        try:
            rows = database.query(sql, parameters)
        except ReadOnlyDatabaseError as exc:
            raise SchemaAdapterError(exc.error_code, f"failed to read message table {candidate.table}") from exc

        messages: list[SourceMessageRow] = []
        for row in rows:
            raw = dict(row)
            msg_time = _integer(raw.get(mapping["msg_time"]))
            msg_seq = _integer(raw.get(mapping["msg_seq"])) if "msg_seq" in mapping else 0
            msg_id = str(raw.get(mapping["msg_id"]) or "")
            row_identity = str(raw.pop("__row_identity", "") or msg_id)
            chat_type_value = raw.get(mapping["chat_type"]) if "chat_type" in mapping else candidate.chat_kind
            messages.append(
                SourceMessageRow(
                    source_table=candidate.table,
                    source_primary_key=row_identity,
                    msg_time=msg_time,
                    msg_seq=msg_seq,
                    msg_id=msg_id,
                    conversation_id=str(raw.get(mapping["conversation_id"]) or ""),
                    sender_id=str(raw.get(mapping["sender_id"]) or ""),
                    chat_type=str(chat_type_value or candidate.chat_kind),
                    content=raw.get(mapping["content"]),
                    raw_columns=raw,
                )
            )

        next_cursor = cursor
        if messages:
            last = messages[-1]
            next_cursor = IncrementalCursor(
                last_msg_time=last.msg_time,
                last_msg_seq=last.msg_seq,
                last_msg_id=last.msg_id,
                last_row_identity=last.source_primary_key,
            )
        return MessagePage(tuple(messages), next_cursor)
