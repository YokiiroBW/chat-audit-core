from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from collector.qqnt.reader import ReadOnlySQLiteDatabase


MESSAGE_COLUMN_ALIASES = {
    "msg_time": ("msgtime", "msg_time", "timestamp", "send_time", "time"),
    "msg_seq": ("msgseq", "msg_seq", "sequence", "seq"),
    "msg_id": ("msgid", "msg_id", "message_id", "id"),
    "conversation_id": ("peeruid", "conversation_id", "chat_id", "group_id", "friend_id", "peer_id"),
    "sender_id": ("senderuid", "sender_id", "sender", "from_uid", "from_id"),
    "content": ("msgcontent", "msg_content", "content", "payload", "body"),
    "chat_type": ("chattype", "chat_type", "message_type", "type"),
}


@dataclass(frozen=True)
class TableSchema:
    name: str
    columns: tuple[str, ...]
    primary_key: tuple[str, ...]
    without_rowid: bool


@dataclass(frozen=True)
class MessageTableCandidate:
    table: str
    score: int
    mapping: dict[str, str]
    chat_kind: str


@dataclass(frozen=True)
class SchemaProbeReport:
    sqlite_version: str
    user_version: int
    application_id: int
    tables: tuple[TableSchema, ...]
    message_candidates: tuple[MessageTableCandidate, ...]
    fingerprint: str


def _quote_identifier(identifier: str) -> str:
    if not re.fullmatch(r"[^\x00]+", identifier):
        raise ValueError("invalid SQLite identifier")
    return '"' + identifier.replace('"', '""') + '"'


def _map_message_columns(columns: tuple[str, ...]) -> tuple[int, dict[str, str]]:
    by_lower = {column.lower(): column for column in columns}
    mapping: dict[str, str] = {}
    for logical, aliases in MESSAGE_COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in by_lower:
                mapping[logical] = by_lower[alias]
                break
    required = {"msg_time", "msg_id", "conversation_id", "sender_id", "content"}
    score = len(mapping) + (5 if required.issubset(mapping) else 0)
    return score, mapping


def _map_qqnt_numeric_columns(table_name: str, columns: tuple[str, ...]) -> tuple[int, dict[str, str]]:
    if table_name not in {"c2c_msg_table", "group_msg_table"}:
        return 0, {}
    required = {"40001", "40003", "40020", "40021", "40050", "40800"}
    if not required.issubset(columns):
        return 0, {}
    return 11, {
        "msg_id": "40001",
        "msg_seq": "40003",
        "msg_time": "40050",
        "conversation_id": "40021",
        "sender_id": "40020",
        "content": "40800",
    }


def _chat_kind(table_name: str, mapping: dict[str, str]) -> str:
    lowered = table_name.lower()
    if "group" in lowered:
        return "group"
    if "c2c" in lowered or "friend" in lowered or "private" in lowered:
        return "private"
    return "dynamic" if "chat_type" in mapping else "unknown"


def probe_schema(database: ReadOnlySQLiteDatabase) -> SchemaProbeReport:
    with database.read_transaction() as connection:
        sqlite_version = str(connection.execute("SELECT sqlite_version()").fetchone()[0])
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
        master_rows = connection.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        tables: list[TableSchema] = []
        candidates: list[MessageTableCandidate] = []
        for master_row in master_rows:
            name = str(master_row["name"])
            info_rows = connection.execute(f"PRAGMA table_info({_quote_identifier(name)})").fetchall()
            columns = tuple(str(row["name"]) for row in info_rows)
            primary_key = tuple(
                str(row["name"])
                for row in sorted(info_rows, key=lambda row: int(row["pk"]))
                if int(row["pk"]) > 0
            )
            sql = str(master_row["sql"] or "")
            tables.append(TableSchema(name, columns, primary_key, "WITHOUT ROWID" in sql.upper()))
            score, mapping = _map_message_columns(columns)
            qqnt_score, qqnt_mapping = _map_qqnt_numeric_columns(name, columns)
            if qqnt_score > score:
                score, mapping = qqnt_score, qqnt_mapping
            if score >= 10:
                candidates.append(MessageTableCandidate(name, score, mapping, _chat_kind(name, mapping)))

    canonical = {
        "user_version": user_version,
        "application_id": application_id,
        "tables": [
            {
                "name": table.name,
                "columns": table.columns,
                "primary_key": table.primary_key,
                "without_rowid": table.without_rowid,
            }
            for table in tables
        ],
    }
    fingerprint = hashlib.sha256(
        json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return SchemaProbeReport(
        sqlite_version=sqlite_version,
        user_version=user_version,
        application_id=application_id,
        tables=tuple(tables),
        message_candidates=tuple(sorted(candidates, key=lambda item: (-item.score, item.table))),
        fingerprint=fingerprint,
    )
