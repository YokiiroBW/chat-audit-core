from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from collector.qqnt.reader import ReadOnlySQLiteDatabase
from collector.qqnt.schema_probe import MessageTableCandidate, SchemaProbeReport
from collector.sync.cursor import IncrementalCursor


class SchemaAdapterError(RuntimeError):
    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


@dataclass(frozen=True)
class SourceMessageRow:
    source_table: str
    source_primary_key: str
    msg_time: int
    msg_seq: int
    msg_id: str
    conversation_id: str
    sender_id: str
    chat_type: str
    content: Any
    raw_columns: dict[str, Any]


@dataclass(frozen=True)
class MessagePage:
    rows: tuple[SourceMessageRow, ...]
    next_cursor: IncrementalCursor


class QQNTSchemaAdapter(Protocol):
    name: str

    def supports(self, report: SchemaProbeReport) -> bool: ...

    def candidates(self, report: SchemaProbeReport) -> tuple[MessageTableCandidate, ...]: ...

    def read_page(
        self,
        database: ReadOnlySQLiteDatabase,
        candidate: MessageTableCandidate,
        cursor: IncrementalCursor,
        *,
        limit: int,
        overlap_seconds: int = 0,
    ) -> MessagePage: ...

    def cursor_after(
        self,
        database: ReadOnlySQLiteDatabase,
        candidate: MessageTableCandidate,
        value: IncrementalCursor,
        baseline: IncrementalCursor,
    ) -> bool: ...
