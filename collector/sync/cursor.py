from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from collector.sync.state import CollectorStateStore


@dataclass(frozen=True, order=True)
class IncrementalCursor:
    last_msg_time: int = 0
    last_msg_seq: int = 0
    last_msg_id: str = ""
    last_row_identity: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> "IncrementalCursor":
        if not value:
            return cls()
        return cls(
            last_msg_time=int(value.get("last_msg_time") or 0),
            last_msg_seq=int(value.get("last_msg_seq") or 0),
            last_msg_id=str(value.get("last_msg_id") or ""),
            last_row_identity=str(value.get("last_row_identity") or ""),
        )

    def overlap_start(self, seconds: int) -> int:
        scale = 1000 if self.last_msg_time > 100_000_000_000 else 1
        return max(0, self.last_msg_time - max(0, seconds) * scale)


class CursorRepository:
    def __init__(self, store: CollectorStateStore, source_id: str) -> None:
        self.store = store
        self.source_id = source_id

    def load_table(self, table_name: str) -> IncrementalCursor:
        return IncrementalCursor.from_dict(
            self.store.get_cursor(source_id=self.source_id, scope="table", name=table_name)
        )

    def save_table(self, table_name: str, cursor: IncrementalCursor) -> None:
        self.store.set_cursor(
            source_id=self.source_id,
            scope="table",
            name=table_name,
            value=cursor.to_dict(),
        )

    def load_conversation(self, conversation_id: str) -> IncrementalCursor:
        return IncrementalCursor.from_dict(
            self.store.get_cursor(source_id=self.source_id, scope="conversation", name=conversation_id)
        )

    def save_conversation(self, conversation_id: str, cursor: IncrementalCursor) -> None:
        self.store.set_cursor(
            source_id=self.source_id,
            scope="conversation",
            name=conversation_id,
            value=cursor.to_dict(),
        )
