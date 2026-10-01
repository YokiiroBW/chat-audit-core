"""Read stored evidence without the console's hydration or cross-message expansion.

This is an archive lookup, not a Core revision or Memory permission verifier.
Callers must supply a server-resolved scope, never a user-chosen robot/room.
"""

from dataclasses import dataclass
from typing import Literal

from sqlalchemy import LargeBinary, and_, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.message_scope import apply_robot_message_scope
from app.models import ImportSource, Message, MessageSourceRecord

MAX_RECORD_CHARS = 16384


@dataclass(frozen=True)
class ArchivedMessage:
    msg_hash: str
    external_message_id: str | None
    sender_id: str
    timestamp: int
    source_sequence: int | None
    raw_message: str
    raw_chars: int
    raw_bytes: int


@dataclass(frozen=True)
class ArchiveScope:
    robot_id: str
    room_id: str
    message_type: Literal["group", "private"]
    id_kind: Literal["external", "qqnt_source", "qqnt_platform"] = "external"
    import_source_id: str | None = None
    platform: Literal["qq"] = "qq"

    def __post_init__(self):
        if (
            not self.robot_id or not self.room_id or self.platform != "qq"
            or self.message_type not in {"group", "private"}
            or self.id_kind not in {"external", "qqnt_source", "qqnt_platform"}
            or (self.id_kind != "external" and not self.import_source_id)
            or (self.id_kind == "external" and self.import_source_id is not None)
        ):
            raise ValueError("An exact supported archive scope is required")


@dataclass(frozen=True)
class EvidenceWindow:
    target: ArchivedMessage
    before: list[ArchivedMessage]
    after: list[ArchivedMessage]


def _scoped_messages(scope: ArchiveScope, dialect: str):
    # Bound materialization inside SQL, before Python loads a large archive row.
    # raw_chars is checked before any output; the prefix is never sent as a
    # complete message. Do not load local_message or source payload JSON.
    stmt = select(
        Message.msg_hash, Message.external_message_id, Message.sender_id,
        Message.timestamp, Message.source_sequence,
        func.substr(Message.raw_message, 1, MAX_RECORD_CHARS + 1).label("raw_message"),
        func.length(Message.raw_message).label("raw_chars"),
        (func.length(cast(Message.raw_message, LargeBinary)) if dialect == "sqlite"
         else func.octet_length(Message.raw_message)).label("raw_bytes"),
    )
    return apply_robot_message_scope(stmt, scope.robot_id).where(
        Message.platform == scope.platform,
        Message.room_id == scope.room_id,
        Message.message_type == scope.message_type,
    )


def _message_id_filter(scope: ArchiveScope, message_id: str):
    if scope.id_kind == "external":
        return Message.external_message_id == message_id
    # Visibility of the canonical message is insufficient: another account's
    # source alias must never become a lookup key for this account.
    id_column = (
        MessageSourceRecord.source_external_message_id
        if scope.id_kind == "qqnt_source" else MessageSourceRecord.platform_message_id
    )
    return select(MessageSourceRecord.id).join(
        ImportSource, ImportSource.id == MessageSourceRecord.source_id
    ).where(
        MessageSourceRecord.msg_hash == Message.msg_hash,
        ImportSource.id == scope.import_source_id,
        ImportSource.account_id == scope.robot_id,
        ImportSource.platform == scope.platform,
        id_column == message_id,
    ).exists()


class EvidenceService:
    @staticmethod
    async def read(
        db: AsyncSession, *, scope: ArchiveScope, message_id: str,
        before: int = 0, after: int = 0, msg_hash: str | None = None,
    ) -> EvidenceWindow | None:
        if not message_id or any(type(n) is not int or not 0 <= n <= 10 for n in (before, after)):
            raise ValueError("A message id and context bounds of 0..10 are required")
        # Never choose an arbitrary match when native ids have been reused.
        # no_autoflush also ensures this reader cannot flush its caller's writes.
        with db.no_autoflush:
            dialect = db.get_bind().dialect.name
            stmt = _scoped_messages(scope, dialect).where(_message_id_filter(scope, message_id))
            if msg_hash is not None:
                stmt = stmt.where(Message.msg_hash == msg_hash)
            result = await db.execute(stmt.limit(2))
            targets = [ArchivedMessage(**row) for row in result.mappings()]
            if len(targets) != 1:
                return None
            target = targets[0]
            sequence = func.coalesce(Message.source_sequence, -1)
            target_sequence = target.source_sequence if target.source_sequence is not None else -1
            columns = (Message.timestamp, sequence, Message.msg_hash)
            values = (target.timestamp, target_sequence, target.msg_hash)

            async def neighbors(count: int, earlier: bool) -> list[ArchivedMessage]:
                if not count:
                    return []
                compare = [c < v if earlier else c > v for c, v in zip(columns, values)]
                boundary = or_(
                    compare[0], and_(columns[0] == values[0], compare[1]),
                    and_(columns[0] == values[0], columns[1] == values[1], compare[2]),
                )
                rows = await db.execute(
                    _scoped_messages(scope, dialect).where(boundary).order_by(
                        *(c.desc() if earlier else c.asc() for c in columns)
                    ).limit(count)
                )
                messages = [ArchivedMessage(**row) for row in rows.mappings()]
                return list(reversed(messages)) if earlier else messages

            return EvidenceWindow(target, await neighbors(before, True), await neighbors(after, False))
