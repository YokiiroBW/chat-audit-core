from dataclasses import replace

import pytest
from sqlalchemy import event, select

from app.models import ImportSource, Message, MessageSourceRecord, RobotMessage
from app.services.evidence_service import ArchiveScope, EvidenceService
from app.services.message_service import MessageService


def archived(db, key, *, robot="account-a", room="42", kind="group", platform="qq", message_id="7", timestamp=100, sequence=None):
    row = Message(
        msg_hash=key, platform=platform, room_id=room, message_type=kind,
        external_message_id=message_id, sender_id="sender-" + key,
        nickname="same display name", raw_message="raw " + key,
        local_message="/media/private-cache-" + key, timestamp=timestamp, source_sequence=sequence,
    )
    db.add(row)
    if robot:
        db.add(RobotMessage(robot_id=robot, msg_hash=key))
    return row


def imported(db, key, *, account="account-a", source="source-a", source_alias="native-7", platform_alias="platform-7"):
    db.add(ImportSource(id=source, source_type="qqnt", platform="qq", account_id=account, device_id=source))
    db.add(MessageSourceRecord(
        msg_hash=key, source_id=source, source_table="messages", source_primary_key=key,
        source_external_message_id=source_alias, platform_message_id=platform_alias,
    ))


@pytest.mark.asyncio
async def test_exact_account_platform_room_and_conversation_type(db_session):
    archived(db_session, "a")
    archived(db_session, "b", robot="account-b")
    archived(db_session, "private", kind="private")
    archived(db_session, "other-room", room="43")
    archived(db_session, "telegram", platform="tg")
    await db_session.commit()
    scope = ArchiveScope("account-a", "42", "group")
    for actual_scope, expected in [
        (scope, "a"), (replace(scope, robot_id="account-b"), "b"),
        (replace(scope, message_type="private"), "private"),
        (replace(scope, room_id="43"), "other-room"),
    ]:
        result = await EvidenceService.read(db_session, scope=actual_scope, message_id="7", before=10, after=10)
        assert [m.msg_hash for m in [*result.before, result.target, *result.after]] == [expected]
    assert await EvidenceService.read(db_session, scope=replace(scope, robot_id="unknown"), message_id="7") is None
    assert await EvidenceService.read(db_session, scope=scope, message_id="missing") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("id_kind,message_id", [("qqnt_source", "native-7"), ("qqnt_platform", "platform-7")])
async def test_import_only_visibility_and_alias_ownership(db_session, id_kind, message_id):
    archived(db_session, "import-only", robot=None, message_id=None)
    imported(db_session, "import-only")
    # One canonical row visible to A must not expose B's private alias.
    imported(db_session, "import-only", account="account-b", source="source-b", source_alias="b-only", platform_alias="b-only")
    await db_session.commit()
    scope = ArchiveScope("account-a", "42", "group", id_kind, "source-a")
    assert (await EvidenceService.read(db_session, scope=scope, message_id=message_id)).target.msg_hash == "import-only"
    for wrong in [replace(scope, robot_id="unknown"), replace(scope, import_source_id="source-b")]:
        assert await EvidenceService.read(db_session, scope=wrong, message_id=message_id) is None
        assert await EvidenceService.read(db_session, scope=wrong, message_id="b-only") is None
    assert await EvidenceService.read(db_session, scope=replace(scope, id_kind="external", import_source_id=None), message_id=message_id) is None


@pytest.mark.asyncio
async def test_ambiguous_native_id_is_unavailable(db_session):
    archived(db_session, "first")
    archived(db_session, "reused-id")
    await db_session.commit()
    assert await EvidenceService.read(db_session, scope=ArchiveScope("account-a", "42", "group"), message_id="7") is None


@pytest.mark.asyncio
async def test_context_stable_order_and_no_cross_scope_expansion(db_session):
    for key, seq in [("a", None), ("b", 0), ("c", 0), ("d", 1), ("e", 2)]:
        archived(db_session, key, message_id=key, sequence=seq)
    archived(db_session, "foreign", robot="account-b", message_id="foreign", sequence=1)
    archived(db_session, "private", kind="private", message_id="private", sequence=1)
    await db_session.commit()
    result = await EvidenceService.read(db_session, scope=ArchiveScope("account-a", "42", "group"), message_id="c", before=2, after=1)
    assert [m.msg_hash for m in result.before] == ["a", "b"]
    assert [m.msg_hash for m in result.after] == ["d"]
    assert not hasattr(result.target, "reply_preview")
    assert not hasattr(result.target, "media_references")


@pytest.mark.asyncio
async def test_repeat_event_and_read_are_idempotent_and_never_flush(db_session):
    payload = dict(room_id="42", message_type="group", sender_id="sender", message_id="7", raw_message="original", timestamp=100)
    first = await MessageService.process_incoming_message(db_session, "account-a", "qq", payload)
    second = await MessageService.process_incoming_message(db_session, "account-a", "qq", payload)
    assert first == second
    statements = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    bind = db_session.bind.sync_engine
    event.listen(bind, "before_cursor_execute", capture)
    try:
        scope = ArchiveScope("account-a", "42", "group")
        one = await EvidenceService.read(db_session, scope=scope, message_id="7")
        # The reader must not persist an unrelated pending caller mutation.
        (await db_session.get(Message, first)).nickname = "pending"
        two = await EvidenceService.read(db_session, scope=scope, message_id="7", before=10, after=10)
        assert one.target.msg_hash == two.target.msg_hash == first
        assert all(s.lstrip().upper().startswith("SELECT") for s in statements)
    finally:
        event.remove(bind, "before_cursor_execute", capture)
        await db_session.rollback()
    assert len((await db_session.execute(select(Message))).scalars().all()) == 1
    assert len((await db_session.execute(select(RobotMessage))).scalars().all()) == 1


@pytest.mark.parametrize("scope", [
    {"robot_id": ""}, {"room_id": ""}, {"platform": "tg"}, {"message_type": "unknown"},
    {"id_kind": "anything"}, {"id_kind": "qqnt_source"}, {"import_source_id": "unexpected"},
])
def test_scope_must_be_explicit_and_supported(scope):
    with pytest.raises(ValueError):
        ArchiveScope(**{ "robot_id": "account-a", "room_id": "42", "message_type": "group", **scope})


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [-1, 11, True, 1.5])
async def test_context_limits(db_session, count):
    with pytest.raises(ValueError):
        await EvidenceService.read(db_session, scope=ArchiveScope("account-a", "42", "group"), message_id="7", before=count)
