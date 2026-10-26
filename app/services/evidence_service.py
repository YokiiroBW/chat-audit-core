"""Read stored evidence without the console's hydration or cross-message expansion.

This is an archive lookup, not a Core revision or Memory permission verifier.
Callers must supply a server-resolved scope, never a user-chosen robot/room.
"""

from dataclasses import dataclass
from typing import Literal

from sqlalchemy import LargeBinary, and_, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.message_scope import apply_robot_message_scope
from app.models import ImportSource, Message, MessageSourceRecord

MAX_RECORD_CHARS = 16384
MAX_NATIVE_ALIASES = 256


class ArchiveLimitExceeded(ValueError):
    """An archive response cannot be provided completely within its bound."""


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


@dataclass(frozen=True)
class NativeAlias:
    id_kind: Literal["external", "qqnt_source", "qqnt_platform"]
    message_id: str
    import_source_id: str | None


@dataclass(frozen=True)
class EvidencePage:
    messages: list[ArchivedMessage]
    has_more: bool


def apply_archive_scope(stmt, scope: ArchiveScope):
    """The registered account and, when specified, the exact import source.

    The account visibility join/EXISTS remains the original message_scope rule.
    Import mappings additionally restrict every search, neighbor and relation
    candidate, not only the alias used to find the center message.
    """
    stmt = apply_robot_message_scope(stmt, scope.robot_id).where(
        Message.platform == scope.platform,
        Message.room_id == scope.room_id,
        Message.message_type == scope.message_type,
    )
    if scope.import_source_id is not None:
        source_record, import_source = aliased(MessageSourceRecord), aliased(ImportSource)
        stmt = stmt.where(select(source_record.id).join(
            import_source, import_source.id == source_record.source_id,
        ).where(
            source_record.msg_hash == Message.msg_hash,
            import_source.id == scope.import_source_id,
            import_source.account_id == scope.robot_id,
            import_source.platform == scope.platform,
        ).exists())
    return stmt


def message_order():
    return Message.timestamp, func.coalesce(Message.source_sequence, -1), Message.msg_hash


def message_key(message: ArchivedMessage):
    return message.timestamp, message.source_sequence if message.source_sequence is not None else -1, message.msg_hash


def keyset_boundary(key, *, earlier=True):
    columns = message_order()
    comparisons = [column < value if earlier else column > value for column, value in zip(columns, key)]
    return or_(
        comparisons[0], and_(columns[0] == key[0], comparisons[1]),
        and_(columns[0] == key[0], columns[1] == key[1], comparisons[2]),
    )


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
    return apply_archive_scope(stmt, scope)


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
        db: AsyncSession, *, scope: ArchiveScope, message_id: str | None,
        before: int = 0, after: int = 0, msg_hash: str | None = None,
    ) -> EvidenceWindow | None:
        if (not message_id and not msg_hash) or any(type(n) is not int or not 0 <= n <= 10 for n in (before, after)):
            raise ValueError("A message id and context bounds of 0..10 are required")
        # Never choose an arbitrary match when native ids have been reused.
        # no_autoflush also ensures this reader cannot flush its caller's writes.
        with db.no_autoflush:
            dialect = db.get_bind().dialect.name
            stmt = _scoped_messages(scope, dialect)
            if message_id is not None:
                stmt = stmt.where(_message_id_filter(scope, message_id))
            if msg_hash is not None:
                stmt = stmt.where(Message.msg_hash == msg_hash)
            result = await db.execute(stmt.limit(2))
            targets = [ArchivedMessage(**row) for row in result.mappings()]
            if len(targets) != 1:
                return None
            target = targets[0]
            columns = message_order()
            values = message_key(target)

            async def neighbors(count: int, earlier: bool) -> list[ArchivedMessage]:
                if not count:
                    return []
                rows = await db.execute(
                    _scoped_messages(scope, dialect).where(keyset_boundary(values, earlier=earlier)).order_by(
                        *(c.desc() if earlier else c.asc() for c in columns)
                    ).limit(count)
                )
                messages = [ArchivedMessage(**row) for row in rows.mappings()]
                return list(reversed(messages)) if earlier else messages

            return EvidenceWindow(target, await neighbors(before, True), await neighbors(after, False))

    @staticmethod
    async def search(
        db: AsyncSession, *, scope: ArchiveScope, keyword: str | None = None,
        sender_id: str | None = None, start_timestamp: int | None = None,
        end_timestamp: int | None = None, limit: int = 50, before=None,
    ) -> EvidencePage:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Search limit must be 1..100")
        if keyword is not None and (not isinstance(keyword, str) or len(keyword) > 256):
            raise ValueError("Invalid search keyword")
        if start_timestamp is not None and end_timestamp is not None and start_timestamp > end_timestamp:
            raise ValueError("Invalid search interval")
        with db.no_autoflush:
            stmt = _scoped_messages(scope, db.get_bind().dialect.name)
            if keyword:
                stmt = stmt.where(func.lower(Message.raw_message).contains(keyword.lower(), autoescape=True))
            if sender_id is not None:
                stmt = stmt.where(Message.sender_id == sender_id)
            if start_timestamp is not None:
                stmt = stmt.where(Message.timestamp >= start_timestamp)
            if end_timestamp is not None:
                stmt = stmt.where(Message.timestamp <= end_timestamp)
            if before is not None:
                stmt = stmt.where(keyset_boundary(before))
            result = await db.execute(stmt.order_by(*(c.desc() for c in message_order())).limit(limit + 1))
            messages = [ArchivedMessage(**row) for row in result.mappings()]
            return EvidencePage(messages[:limit], len(messages) > limit)

    @staticmethod
    async def current(db: AsyncSession, *, scope: ArchiveScope, msg_hash: str) -> ArchivedMessage | None:
        with db.no_autoflush:
            result = await db.execute(_scoped_messages(scope, db.get_bind().dialect.name).where(
                Message.msg_hash == msg_hash,
            ).limit(1))
            row = result.mappings().first()
            return ArchivedMessage(**row) if row is not None else None

    @staticmethod
    async def current_many(db: AsyncSession, *, scope: ArchiveScope, msg_hashes):
        result = {}
        keys = sorted(set(msg_hashes))
        with db.no_autoflush:
            for start in range(0, len(keys), 256):
                rows = await db.execute(_scoped_messages(scope, db.get_bind().dialect.name).where(
                    Message.msg_hash.in_(keys[start:start + 256]),
                ))
                result.update((row['msg_hash'], ArchivedMessage(**row)) for row in rows.mappings())
        return result

    @staticmethod
    async def native_aliases(db: AsyncSession, *, scope: ArchiveScope, messages: list[ArchivedMessage]):
        """Return actual aliases only from the registered account/source.

        Complete aliases matter to the caller's explicit-forget filtering. An
        oversized alias set fails; it is never silently sliced into a subset.
        """
        aliases = {message.msg_hash: set() for message in messages}
        for message in messages:
            if message.external_message_id:
                aliases[message.msg_hash].add(NativeAlias("external", message.external_message_id, None))
        if not messages:
            return aliases
        with db.no_autoflush:
            stmt = apply_archive_scope(select(
                MessageSourceRecord.msg_hash, MessageSourceRecord.source_id,
                MessageSourceRecord.source_external_message_id, MessageSourceRecord.platform_message_id,
            ).join(Message, Message.msg_hash == MessageSourceRecord.msg_hash).join(
                ImportSource, ImportSource.id == MessageSourceRecord.source_id,
            ), scope).where(
                Message.msg_hash.in_(aliases), ImportSource.account_id == scope.robot_id,
                ImportSource.platform == scope.platform,
            )
            if scope.import_source_id is not None:
                stmt = stmt.where(ImportSource.id == scope.import_source_id)
            distinct = stmt.distinct().subquery()
            counts = await db.execute(select(distinct.c.msg_hash, func.count()).group_by(distinct.c.msg_hash))
            if any(count > MAX_NATIVE_ALIASES for _, count in counts):
                raise ArchiveLimitExceeded("Too many native aliases")
            rows = await db.execute(stmt.distinct())
            for msg_hash, source, source_id, platform_id in rows:
                for kind, value in (("qqnt_source", source_id), ("qqnt_platform", platform_id)):
                    if value:
                        aliases[msg_hash].add(NativeAlias(kind, value, source))
        if any(len(values) > MAX_NATIVE_ALIASES for values in aliases.values()):
            raise ArchiveLimitExceeded("Too many native aliases")
        return aliases
