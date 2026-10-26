import json

import pytest
from sqlalchemy import delete

from app.models import ImportSource, Message, MessagePart, MessageSourceRecord
from app.services.evidence_service import ArchiveScope
from app.services.interaction_service import InteractionService
from tests.test_evidence_api import CHANNEL, api_client, configuration
from tests.test_evidence_service import archived


def relation(db, key, kind, data, ordinal):
    db.add(MessagePart(msg_hash=key, ordinal=ordinal, part_type=kind,
                       payload_json=json.dumps(data), source_format="onebot"))


def stats_request(**changes):
    return {"schema_version": 1, "channel": CHANNEL, "start_timestamp": 0, "end_timestamp": 7200,
            "bucket_seconds": 3600, "timezone_offset_minutes": 0, "message_limit": 10000, **changes}


async def observe(db, scope=None, **changes):
    options = {"start_timestamp": 0, "end_timestamp": 7200, **changes}
    return await InteractionService.observe(db, scope=scope or ArchiveScope("account-a", "42", "group"), **options)


@pytest.mark.asyncio
async def test_reply_and_mention_same_target_count_once_with_broadcast_and_bucket(db_session, tmp_path):
    archived(db_session, "b", message_id="7", timestamp=0).sender_id = "B"
    archived(db_session, "a", message_id="8", timestamp=3600).sender_id = "A"
    for ordinal, (kind, data) in enumerate([
        ("reply", {"id": "7"}), ("at", {"qq": "B"}), ("at", {"qq": "B"}), ("at", {"qq": "all"}),
    ]):
        relation(db_session, "a", kind, data, ordinal)
    await db_session.commit()
    client, _ = api_client(db_session, tmp_path)
    async with client:
        response = await client.post("/internal/evidence/group-stats", json=stats_request())
        assert response.status_code == 200, response.text
        value = response.json()
        assert value["authors"] == [{"sender_id": "A", "messages": 1}, {"sender_id": "B", "messages": 1}]
        edge = value["edges"][0]
        assert {k: v for k, v in edge.items() if k != "evidence_locators"} == {
            "from_sender_id": "A", "to_sender_id": "B", "reply_count": 1,
            "mention_count": 1, "interaction_count": 1}
        assert len(edge["evidence_locators"]) == 1
        assert value["buckets"] == [
            {"start_timestamp": 0, "messages": 1, "active_senders": 1, "reply_messages": 0,
             "mention_messages": 0, "broadcast_messages": 0},
            {"start_timestamp": 3600, "messages": 1, "active_senders": 1, "reply_messages": 1,
             "mention_messages": 1, "broadcast_messages": 1}]
        assert value["coverage"]["complete"] and value["coverage"]["broadcast_messages"] == 1
        # Returned evidence remains an actual same-scope archive locator.
        locator = edge["evidence_locators"][0]
        read = await client.post("/internal/evidence/read-locator", json={"schema_version": 1,
            "channel": CHANNEL, "locator": locator, "expected_stored_content_sha256": locator.split(":")[-1],
            "before": 0, "after": 0})
        assert read.json()["status"] == "archive_current" and read.json()["target"]["sender_id"] == "A"


@pytest.mark.asyncio
async def test_reused_reply_id_is_unresolved_not_latest_and_foreign_does_not_expand(db_session):
    archived(db_session, "target", message_id="7", timestamp=1).sender_id = "B"
    reply = archived(db_session, "reply", message_id="8", timestamp=10)
    reply.sender_id, reply.raw_message = "A", "reply [CQ:reply,id=7] [CQ:at,qq=not-a-known-author]"
    for key, options in [("foreign", {"robot": "account-b"}), ("private", {"kind": "private"}),
                         ("room", {"room": "other"}), ("platform", {"platform": "tg"})]:
        archived(db_session, key, message_id="7", timestamp=5, **options)
    await db_session.commit()
    first = await observe(db_session)
    assert len(first["edges"]) == 1 and first["edges"][0]["to_sender_id"] == "B"
    assert first["coverage"]["messages_scanned"] == 2 and first["coverage"]["unresolved_mention"] == 1
    archived(db_session, "reused", message_id="7", timestamp=20).sender_id = "newest-wrong-person"
    await db_session.commit()
    second = await observe(db_session)
    assert second["edges"] == [] and second["coverage"]["unresolved_reply"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,target", [("qqnt_source", "source-7"), ("qqnt_platform", "platform-7")])
async def test_exact_import_source_resolves_reply_without_foreign_alias(db_session, kind, target):
    db_session.add_all([
        ImportSource(id="source-a", account_id="account-a", device_id="a", platform="qq", source_type="qqnt"),
        ImportSource(id="source-b", account_id="account-b", device_id="b", platform="qq", source_type="qqnt"),
        ImportSource(id="device-c", account_id="account-a", device_id="c", platform="qq", source_type="qqnt")])
    for key, source, sender in [("target", "source-a", "B"), ("reply", "source-a", "A"),
                                ("foreign", "source-b", "foreign"), ("device", "device-c", "wrong-device")]:
        archived(db_session, key, robot=None, message_id=None).sender_id = sender
        db_session.add(MessageSourceRecord(msg_hash=key, source_id=source, source_table="messages", source_primary_key=key,
            source_external_message_id="source-7" if key != "reply" else "source-8",
            platform_message_id="platform-7" if key != "reply" else "platform-8"))
    relation(db_session, "reply", "reply", {"id": target}, 0)
    await db_session.commit()
    result = await observe(db_session, ArchiveScope("account-a", "42", "group", kind, "source-a"))
    assert result["coverage"]["messages_scanned"] == 2
    assert len(result["edges"]) == 1 and result["edges"][0]["to_sender_id"] == "B"
    assert {a["sender_id"] for a in result["authors"]} == {"A", "B"}


@pytest.mark.asyncio
async def test_scan_limit_and_late_import_or_delete_are_recomputed_not_watermarked(db_session):
    archived(db_session, "first", message_id="1", timestamp=100)
    await db_session.commit()
    assert (await observe(db_session))["coverage"]["messages_scanned"] == 1
    archived(db_session, "late-old", message_id="2", timestamp=1)
    await db_session.commit()
    second = await observe(db_session)
    assert second["coverage"]["messages_scanned"] == 2 and second["coverage"]["complete"]
    limited = await observe(db_session, message_limit=1)
    assert limited["coverage"]["has_more"] and not limited["coverage"]["complete"]
    assert limited["authors"] == [{"sender_id": "sender-late-old", "messages": 1}]
    await db_session.execute(delete(Message).where(Message.msg_hash == "late-old"))
    await db_session.commit()
    final = await observe(db_session)
    assert final["coverage"]["messages_scanned"] == 1 and final["coverage"]["complete"]


@pytest.mark.asyncio
async def test_typed_text_cq_literal_is_not_an_actual_mention(db_session):
    row = archived(db_session, "a")
    row.raw_message = "I typed [CQ:at,qq=B] as an example"
    relation(db_session, "a", "text", {"text": row.raw_message}, 0)
    archived(db_session, "b", message_id="9").sender_id = "B"
    await db_session.commit()
    result = await observe(db_session)
    assert result["edges"] == [] and result["coverage"]["unresolved_mention"] == 0


@pytest.mark.asyncio
async def test_unavailable_relation_material_and_offset_are_explicit(db_session):
    row = archived(db_session, "long", timestamp=0)
    row.raw_message = "x" * 3000
    await db_session.commit()
    result = await observe(db_session, bucket_seconds=86400, timezone_offset_minutes=480)
    assert result["buckets"][0]["start_timestamp"] == -28800
    assert result["coverage"]["relation_unavailable_messages"] == 1
    assert not result["coverage"]["complete"]


@pytest.mark.asyncio
async def test_group_stats_cannot_select_private_mapping_or_other_robot(db_session, tmp_path):
    config = configuration()
    config["readers"][0]["mapping_ids"].append("private-a")
    client, _ = api_client(db_session, tmp_path, config)
    async with client:
        private = {**CHANNEL, "channel_conversation_id": "private:42"}
        response = await client.post("/internal/evidence/group-stats", json=stats_request(channel=private))
        assert response.status_code == 404
        response = await client.post("/internal/evidence/group-stats", json={**stats_request(), "robot_id": "account-b"})
        assert response.status_code == 422
