from __future__ import annotations

import argparse
import asyncio
import json
from collections import defaultdict

from sqlalchemy import text

from app.database import AsyncSessionLocal

# The merge statements below use PostgreSQL's DELETE ... USING and aliased
# UPDATE, which SQLite does not accept. Running them elsewhere would fail
# partway through a destructive repair, so the dialect is checked up front.
SUPPORTED_DIALECTS = {"postgresql"}


def _numeric(value: object) -> str | None:
    normalized = str(value or "").strip()
    return normalized if normalized.isdigit() and 5 <= len(normalized) <= 12 else None


def _canonical_identity(row: dict[str, object], account_id: str) -> tuple[str, str, bool | None]:
    raw = json.loads(row["raw_columns_json"] or "{}")
    message_type = str(row["message_type"] or "private")
    room_id = str(row["room_id"])
    sender_id = str(row["sender_id"])
    if message_type == "private":
        peer_id = _numeric(raw.get("40030")) or (room_id if room_id.isdigit() else None)
        sender_identity = str(raw.get("40020") or "").strip()
        conversation_identity = str(raw.get("40021") or "").strip()
        outgoing = sender_identity != conversation_identity if sender_identity and conversation_identity else None
        if peer_id:
            return peer_id, account_id if outgoing else peer_id, outgoing
        return room_id, sender_id, outgoing
    canonical_room = _numeric(raw.get("40030")) or room_id
    canonical_sender = _numeric(raw.get("40033")) or sender_id
    return canonical_room, canonical_sender, canonical_sender == account_id


async def repair(*, account_id: str, apply: bool) -> None:
    async with AsyncSessionLocal() as db:
        dialect = db.get_bind().dialect.name
        if dialect not in SUPPORTED_DIALECTS:
            raise SystemExit(
                f"this repair script only supports {'/'.join(sorted(SUPPORTED_DIALECTS))}, "
                f"but the configured database is {dialect}; refusing to start a destructive "
                "repair that would fail halfway through"
            )
        result = await db.execute(text("""
            select m.msg_hash, m.platform, m.room_id, m.message_type, m.sender_id,
                   m.is_outgoing, m.local_message, m.raw_message, m.timestamp,
                   m.created_at, s.raw_columns_json
            from messages m
            join message_source_records s on s.msg_hash = m.msg_hash
            join import_sources i on i.id = s.source_id
            where i.source_type = 'qqnt_local_db' and i.account_id = :account_id
            order by m.timestamp asc, m.created_at asc, m.msg_hash asc
        """), {"account_id": account_id})
        rows = [dict(row._mapping) for row in result.fetchall()]
        unique_rows = {}
        for row in rows:
            unique_rows.setdefault(str(row["msg_hash"]), row)
        rows = list(unique_rows.values())
        changed_identity = 0
        groups: dict[tuple[str, str, str, str, int, str], list[dict[str, object]]] = defaultdict(list)
        for row in rows:
            room_id, sender_id, outgoing = _canonical_identity(row, account_id)
            if room_id != row["room_id"] or sender_id != row["sender_id"] or outgoing != row["is_outgoing"]:
                changed_identity += 1
            key = (str(row["platform"]), room_id, str(row["message_type"]), sender_id, int(row["timestamp"]), str(row["local_message"] or ""))
            groups[key].append(row | {"canonical_room": room_id, "canonical_sender": sender_id, "canonical_outgoing": outgoing})
        duplicate_groups = [group for group in groups.values() if len(group) > 1]
        print(json.dumps({"account_id": account_id, "qqnt_rows": len(rows), "identity_rows_to_update": changed_identity, "duplicate_groups": len(duplicate_groups), "duplicate_rows_to_merge": sum(len(group) - 1 for group in duplicate_groups), "apply": apply}, ensure_ascii=False))
        if not apply:
            return
        merged = updated = 0
        for group in groups.values():
            canonical = group[0]
            target_hash = str(canonical["msg_hash"])
            for row in group[1:]:
                old_hash = str(row["msg_hash"])
                await db.execute(text("update message_source_records set msg_hash = :target where msg_hash = :old"), {"target": target_hash, "old": old_hash})
                await db.execute(text("update robot_messages set msg_hash = :target where msg_hash = :old and not exists (select 1 from robot_messages r2 where r2.robot_id = robot_messages.robot_id and r2.msg_hash = :target)"), {"target": target_hash, "old": old_hash})
                await db.execute(text("delete from robot_messages where msg_hash = :old"), {"old": old_hash})
                await db.execute(text("update message_parts p set media_reference_id = (select min(t.id) from message_media_references t where t.msg_hash = :target and t.ordinal = (select o.ordinal from message_media_references o where o.id = p.media_reference_id)) where p.msg_hash = :old and p.media_reference_id is not null"), {"target": target_hash, "old": old_hash})
                await db.execute(text("delete from message_media_references o using message_media_references t where o.msg_hash = :old and t.msg_hash = :target and o.ordinal = t.ordinal"), {"target": target_hash, "old": old_hash})
                await db.execute(text("update message_media_references set msg_hash = :target where msg_hash = :old"), {"target": target_hash, "old": old_hash})
                await db.execute(text("delete from message_parts o using message_parts t where o.msg_hash = :old and t.msg_hash = :target and o.ordinal = t.ordinal"), {"target": target_hash, "old": old_hash})
                await db.execute(text("update message_parts set msg_hash = :target where msg_hash = :old"), {"target": target_hash, "old": old_hash})
                await db.execute(text("delete from messages where msg_hash = :old"), {"old": old_hash})
                merged += 1
            await db.execute(text("update messages set room_id = :room_id, sender_id = :sender_id, is_outgoing = :is_outgoing where msg_hash = :msg_hash"), {"room_id": canonical["canonical_room"], "sender_id": canonical["canonical_sender"], "is_outgoing": canonical["canonical_outgoing"], "msg_hash": target_hash})
            updated += 1
        await db.execute(text("update message_parts p set media_reference_id = (select min(r2.id) from message_media_references r2 where r2.msg_hash = p.msg_hash and r2.ordinal = (select r.ordinal from message_media_references r where r.id = p.media_reference_id)) where p.media_reference_id is not null"))
        await db.execute(text("delete from message_media_references p using message_media_references q where p.msg_hash = q.msg_hash and p.ordinal = q.ordinal and p.id > q.id"))
        await db.execute(text("delete from message_parts p using message_parts q where p.msg_hash = q.msg_hash and p.ordinal = q.ordinal and p.id > q.id"))
        await db.commit()
        print(json.dumps({"merged": merged, "updated": updated}, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser(description="Repair QQNT identities and merge exact duplicate messages.")
    # No default: this rewrites and merges one account's archived messages, and
    # a baked-in QQ number meant a mistyped invocation silently repaired somebody
    # else's history.
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    asyncio.run(repair(account_id=args.account_id, apply=args.apply))


if __name__ == "__main__":
    main()
