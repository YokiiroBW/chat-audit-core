"""Bounded archive interaction observations, never personality or affinity facts."""
from collections import Counter, defaultdict
import json
import re

from sqlalchemy import LargeBinary, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ImportSource, Message, MessagePart, MessageSourceRecord
from app.services.evidence_service import (
    ArchiveLimitExceeded, ArchiveScope, apply_archive_scope, message_order,
)
from app.services.query_service import _qqnt_historical_reply_parts

TEXT_CHARS = 2048
PROTO_CHARS = 16384
MAX_RELATION_PARTS = 256
_CQ_RELATION = re.compile(r"\[CQ:(reply|at),([^\[\]]+)\]")


def _chunks(values, size=256):
    values = sorted(values)
    for start in range(0, len(values), size):
        yield values[start:start + size]


async def _parts(db, messages, scope):
    """Read only structured relation payloads; never materialize attachments."""
    hashes = [row.msg_hash for row in messages]
    typed, relations, unavailable = set(), defaultdict(lambda: {"reply": set(), "at": set()}), set()
    for batch in _chunks(hashes):
        with_parts = await db.execute(select(MessagePart.msg_hash).where(
            MessagePart.msg_hash.in_(batch),
        ).distinct())
        typed.update(with_parts.scalars())
        counts = await db.execute(select(MessagePart.msg_hash, func.count()).where(
            MessagePart.msg_hash.in_(batch), MessagePart.part_type.in_(("reply", "at")),
        ).group_by(MessagePart.msg_hash))
        if any(count > MAX_RELATION_PARTS for _, count in counts):
            raise ArchiveLimitExceeded("Too many relation parts")
        rows = await db.execute(select(
            MessagePart.msg_hash, MessagePart.part_type,
            func.substr(MessagePart.payload_json, 1, TEXT_CHARS + 1),
        ).where(MessagePart.msg_hash.in_(batch), MessagePart.part_type.in_(("reply", "at"))))
        for msg_hash, kind, raw in rows:
            if not raw or len(raw) > TEXT_CHARS:
                unavailable.add(msg_hash)
                continue
            try:
                value = json.loads(raw)
            except (ValueError, TypeError):
                unavailable.add(msg_hash)
                continue
            target = value.get("id" if kind == "reply" else "qq") if isinstance(value, dict) else None
            if not isinstance(target, str) or not target or len(target) > 128:
                unavailable.add(msg_hash)
                continue
            relations[msg_hash][kind].add(target)
        # Legacy QQNT reply decoding is the existing pure parser. Only rows of
        # the registered source/account are eligible; no preview is returned.
        stmt = select(
            MessageSourceRecord.msg_hash, func.substr(MessageSourceRecord.raw_40800_protobuf, 1, PROTO_CHARS + 1),
        ).join(ImportSource, ImportSource.id == MessageSourceRecord.source_id).where(
            MessageSourceRecord.msg_hash.in_(batch), ImportSource.account_id == scope.robot_id,
            ImportSource.platform == scope.platform, MessageSourceRecord.raw_40800_protobuf.is_not(None),
        )
        if scope.import_source_id is not None:
            stmt = stmt.where(ImportSource.id == scope.import_source_id)
        distinct = stmt.distinct().subquery()
        counts = await db.execute(select(distinct.c.msg_hash, func.count()).group_by(distinct.c.msg_hash))
        if any(count > MAX_RELATION_PARTS for _, count in counts):
            raise ArchiveLimitExceeded("Too many source relation variants")
        source_rows = await db.execute(stmt.distinct())
        for msg_hash, raw in source_rows:
            if relations[msg_hash]["reply"]:
                continue
            if len(raw) > PROTO_CHARS:
                unavailable.add(msg_hash)
                continue
            for reply in _qqnt_historical_reply_parts(raw):
                relations[msg_hash]["reply"].add(reply["id"])
    for row in messages:
        if row.msg_hash in typed:
            continue
        if (row.raw_chars > TEXT_CHARS or len(row.raw_message.encode("utf-8")) != row.raw_bytes or
                row.raw_message.lstrip().startswith(("{", "[")) and not row.raw_message.lstrip().startswith("[CQ:") or
                re.search(r"\[CQ:(json|xml|node|forward)(?:,|\])", row.raw_message)):
            unavailable.add(row.msg_hash)
            continue
        for match in _CQ_RELATION.finditer(row.raw_message):
            values = dict(item.split("=", 1) for item in match[2].split(",") if "=" in item)
            target = values.get("id" if match[1] == "reply" else "qq")
            if target and len(target) <= 128:
                relations[row.msg_hash][match[1]].add(target)
            else:
                unavailable.add(row.msg_hash)
    return relations, unavailable


async def _reply_targets(db, scope, identifiers):
    """Resolve all candidates; reused native IDs remain ambiguous, never latest."""
    candidates = defaultdict(set)
    for batch in _chunks(identifiers):
        if scope.id_kind == "external":
            stmt = apply_archive_scope(select(
                Message.external_message_id, Message.msg_hash, Message.sender_id,
            ), scope).where(Message.external_message_id.in_(batch))
        else:
            column = (MessageSourceRecord.source_external_message_id
                      if scope.id_kind == "qqnt_source" else MessageSourceRecord.platform_message_id)
            stmt = apply_archive_scope(select(column, Message.msg_hash, Message.sender_id).join(
                MessageSourceRecord, MessageSourceRecord.msg_hash == Message.msg_hash,
            ).join(ImportSource, ImportSource.id == MessageSourceRecord.source_id), scope).where(
                column.in_(batch), ImportSource.id == scope.import_source_id,
                ImportSource.account_id == scope.robot_id, ImportSource.platform == scope.platform,
            )
        rows = await db.execute(stmt.distinct())
        for identifier, msg_hash, sender in rows:
            candidates[identifier].add((msg_hash, sender))
    return {key: next(iter(values))[1] for key, values in candidates.items() if len(values) == 1}


class InteractionService:
    @staticmethod
    async def observe(
        db: AsyncSession, *, scope: ArchiveScope, start_timestamp: int, end_timestamp: int,
        bucket_seconds: int = 3600, timezone_offset_minutes: int = 0, message_limit: int = 10000,
    ) -> dict:
        if scope.message_type != "group":
            raise ValueError("Group statistics require a registered group")
        if (start_timestamp > end_timestamp or bucket_seconds not in {3600, 86400} or
                not -840 <= timezone_offset_minutes <= 840 or not 1 <= message_limit <= 10000):
            raise ValueError("Invalid statistics window")
        with db.no_autoflush:
            stmt = apply_archive_scope(select(
                Message.msg_hash, Message.sender_id, Message.timestamp, Message.source_sequence,
                Message.external_message_id, func.substr(Message.raw_message, 1, TEXT_CHARS).label("raw_message"),
                func.length(Message.raw_message).label("raw_chars"),
                (func.length(cast(Message.raw_message, LargeBinary)) if db.get_bind().dialect.name == "sqlite"
                 else func.octet_length(Message.raw_message)).label("raw_bytes"),
            ), scope).where(Message.timestamp >= start_timestamp, Message.timestamp <= end_timestamp)
            result = await db.execute(stmt.order_by(*(c.asc() for c in message_order())).limit(message_limit + 1))
            messages = list(result.all())
            has_more = len(messages) > message_limit
            messages = messages[:message_limit]
            relations, unavailable = await _parts(db, messages, scope)
            targets = await _reply_targets(db, scope, {
                target for row in messages for target in relations[row.msg_hash]["reply"]
            })
            mentioned = {target for row in messages for target in relations[row.msg_hash]["at"] if target != "all"}
            known = set()
            for batch in _chunks(mentioned):
                rows = await db.execute(apply_archive_scope(select(Message.sender_id), scope).where(
                    Message.sender_id.in_(batch),
                ).distinct())
                known.update(rows.scalars())
            authors, buckets, edges = Counter(), {}, {}
            unresolved_reply = unresolved_mention = broadcast = 0
            offset = timezone_offset_minutes * 60
            for row in messages:
                authors[row.sender_id] += 1
                start = (row.timestamp + offset) // bucket_seconds * bucket_seconds - offset
                bucket = buckets.setdefault(start, {"messages": 0, "senders": set(), "reply_messages": 0,
                                                    "mention_messages": 0, "broadcast_messages": 0})
                bucket["messages"] += 1
                bucket["senders"].add(row.sender_id)
                kinds = relations[row.msg_hash]
                recipient_kinds = defaultdict(set)
                for identifier in kinds["reply"]:
                    if identifier in targets:
                        recipient_kinds[targets[identifier]].add("reply")
                    else:
                        unresolved_reply += 1
                for identifier in kinds["at"]:
                    if identifier == "all":
                        broadcast += 1
                        bucket["broadcast_messages"] += 1
                    elif identifier in known:
                        recipient_kinds[identifier].add("at")
                    else:
                        unresolved_mention += 1
                bucket["reply_messages"] += int(bool(kinds["reply"]))
                bucket["mention_messages"] += int(bool(kinds["at"] - {"all"}))
                for recipient, types in recipient_kinds.items():
                    authors.setdefault(recipient, 0)
                    edge = edges.setdefault((row.sender_id, recipient), {"from_sender_id": row.sender_id,
                        "to_sender_id": recipient, "reply_count": 0, "mention_count": 0,
                        "interaction_count": 0, "evidence_hashes": []})
                    edge["reply_count"] += int("reply" in types)
                    edge["mention_count"] += int("at" in types)
                    edge["interaction_count"] += 1
                    if len(edge["evidence_hashes"]) < 3:
                        edge["evidence_hashes"].append(row.msg_hash)
            return {
                "authors": [{"sender_id": sender, "messages": count} for sender, count in sorted(authors.items())],
                "edges": [edges[key] for key in sorted(edges)],
                "buckets": [{"start_timestamp": start, "messages": value["messages"],
                    "active_senders": len(value["senders"]), "reply_messages": value["reply_messages"],
                    "mention_messages": value["mention_messages"], "broadcast_messages": value["broadcast_messages"]}
                    for start, value in sorted(buckets.items())],
                "coverage": {"consistency": "live_observation", "messages_scanned": len(messages),
                    "has_more": has_more, "complete": not has_more and not unavailable,
                    "unresolved_reply": unresolved_reply, "unresolved_mention": unresolved_mention,
                    "broadcast_messages": broadcast, "relation_unavailable_messages": len(unavailable)},
            }
