import pytest
from sqlalchemy import func, select

from app.models import ImportSource, Message, MessageSourceRecord
from app.services.message_service import MessageService


@pytest.mark.asyncio
async def test_same_message_is_stored_once_but_bound_to_multiple_robot_views(db_session):
    payload = {
        "room_id": "group-10001",
        "message_type": "group",
        "sender_id": "user-42",
        "nickname": "Alice",
        "raw_message": "hello audit system",
        "timestamp": 1783000000,
    }

    first_hash = await MessageService.process_incoming_message(
        db_session, robot_id="robot-a", platform="qq", msg_data=payload
    )
    duplicate_hash = await MessageService.process_incoming_message(
        db_session, robot_id="robot-a", platform="qq", msg_data=payload
    )
    second_robot_hash = await MessageService.process_incoming_message(
        db_session, robot_id="robot-b", platform="qq", msg_data=payload
    )

    assert first_hash == duplicate_hash == second_robot_hash

    messages = await MessageService.list_messages(db_session)
    robot_messages = await MessageService.list_robot_messages(db_session)

    assert len(messages) == 1
    assert messages[0].msg_hash == first_hash
    assert messages[0].local_message == "hello audit system"
    assert {(item.robot_id, item.msg_hash) for item in robot_messages} == {
        ("robot-a", first_hash),
        ("robot-b", first_hash),
    }


@pytest.mark.asyncio
async def test_repeated_same_text_at_different_times_is_stored_as_distinct_messages(db_session):
    base_payload = {
        "room_id": "group-10001",
        "message_type": "group",
        "sender_id": "user-42",
        "nickname": "Alice",
        "raw_message": "same text",
        "timestamp": 1783000000,
    }
    later_payload = {**base_payload, "timestamp": 1783000060}

    first_hash = await MessageService.process_incoming_message(
        db_session, robot_id="robot-a", platform="qq", msg_data=base_payload
    )
    later_hash = await MessageService.process_incoming_message(
        db_session, robot_id="robot-a", platform="qq", msg_data=later_payload
    )

    messages = await MessageService.list_messages(db_session)

    assert first_hash != later_hash
    assert [message.timestamp for message in messages] == [1783000000, 1783000060]


@pytest.mark.asyncio
async def test_same_external_message_id_is_bound_once_across_robot_views(db_session):
    payload = {
        "message_id": "onebot-message-1",
        "room_id": "group-10001",
        "message_type": "group",
        "sender_id": "user-42",
        "nickname": "Alice",
        "raw_message": "same external message",
        "timestamp": 1783000000,
    }

    first_hash = await MessageService.process_incoming_message(
        db_session, robot_id="robot-a", platform="qq", msg_data=payload
    )
    second_robot_hash = await MessageService.process_incoming_message(
        db_session, robot_id="robot-b", platform="qq", msg_data={**payload, "timestamp": 1783000001}
    )

    messages = await MessageService.list_messages(db_session)
    robot_messages = await MessageService.list_robot_messages(db_session)

    assert first_hash == second_robot_hash
    assert len(messages) == 1
    assert messages[0].external_message_id == "onebot-message-1"
    assert {(item.robot_id, item.msg_hash) for item in robot_messages} == {
        ("robot-a", first_hash),
        ("robot-b", first_hash),
    }


@pytest.mark.asyncio
async def test_media_asset_is_content_addressed_and_deduplicated(db_session, tmp_path):
    content = b"fake image bytes"

    first_path = await MessageService.save_media_asset(
        db_session,
        file_content=content,
        file_type="image",
        ext="jpg",
        storage_root=tmp_path,
        public_prefix="/static/storage",
    )
    second_path = await MessageService.save_media_asset(
        db_session,
        file_content=content,
        file_type="image",
        ext="jpg",
        storage_root=tmp_path,
        public_prefix="/static/storage",
    )

    assets = await MessageService.list_media_assets(db_session)

    assert first_path == second_path
    assert len(assets) == 1
    assert assets[0].file_size == len(content)
    assert (tmp_path / first_path.removeprefix("/static/storage/")).exists()


def test_normalize_message_segment_unifies_qqnt_aliases():
    from app.services.message_service import MessageService

    assert MessageService.normalize_message_segment({"type": "quote", "message_id": 123}) == {
        "type": "reply",
        "data": {"message_id": 123, "id": "123"},
    }
    assert MessageService.normalize_message_segment({"type": "mention", "user_id": 456}) == {
        "type": "at",
        "data": {"user_id": 456, "qq": "456"},
    }

def test_normalize_message_segment_turns_napcat_face_payload_into_readable_label():
    from app.services.message_service import MessageService

    normalized = MessageService.normalize_message_segment(
        {
            "type": "face",
            "data": {
                "id": 317,
                "raw": '{"faceIndex": 317, "faceText": "/汪汪"}',
            },
        }
    )

    assert normalized["type"] == "face"
    assert normalized["data"]["id"] == "317"
    assert normalized["data"]["name"] == "/汪汪"
    assert normalized["data"]["faceIndex"] == 317


def test_normalize_message_segment_has_stable_face_fallback_for_object_raw_value():
    from app.services.message_service import MessageService

    normalized = MessageService.normalize_message_segment({"type": "face", "data": {"id": 277, "raw": "[object Object]"}})

    assert normalized == {
        "type": "face",
        "data": {"id": "277", "raw": "[object Object]", "name": "QQ 表情 #277"},
    }


@pytest.mark.asyncio
async def test_realtime_event_converges_with_an_already_imported_message(db_session):
    # A QQNT import and a NapCat capture of the same conversation produced two
    # rows for one message, because the realtime event's connection-scoped id
    # never matches the id the importer recorded.
    source = ImportSource(
        id="qqnt-source-twin",
        source_type="qqnt_local_db",
        platform="qq",
        account_id="robot-a",
        device_id="main-pc",
        status="active",
    )
    imported = Message(
        msg_hash="imported-twin-hash",
        platform="qq",
        room_id="group-twin",
        message_type="group",
        external_message_id="qqnt-9001",
        sender_id="user-a",
        nickname="A",
        raw_message="same message body",
        local_message="same message body",
        timestamp=1783000000,
    )
    db_session.add_all(
        [
            source,
            imported,
            MessageSourceRecord(
                msg_hash=imported.msg_hash,
                source_id=source.id,
                source_table="group_msg_table",
                source_primary_key="row-1",
                source_external_message_id="qqnt-9001",
            ),
        ]
    )
    await db_session.commit()

    msg_hash = await MessageService.process_incoming_message(
        db_session,
        "robot-a",
        "qq",
        {
            "message_id": "napcat-4242",
            "room_id": "group-twin",
            "message_type": "group",
            "sender_id": "user-a",
            "nickname": "A",
            "raw_message": "same message body",
            "timestamp": 1783000000,
        },
    )

    assert msg_hash == "imported-twin-hash"
    assert await db_session.scalar(select(func.count(Message.msg_hash))) == 1


@pytest.mark.asyncio
async def test_two_realtime_events_with_identical_text_stay_separate(db_session):
    # The failure this must not repeat: a member sending the same short text
    # twice within one second is two messages, not one.
    payload = {
        "room_id": "group-echo",
        "message_type": "group",
        "sender_id": "user-a",
        "nickname": "A",
        "raw_message": "+1",
        "timestamp": 1783000000,
    }

    first = await MessageService.process_incoming_message(db_session, "robot-a", "qq", {**payload, "message_id": "napcat-1"})
    second = await MessageService.process_incoming_message(db_session, "robot-a", "qq", {**payload, "message_id": "napcat-2"})

    assert first != second
    assert await db_session.scalar(select(func.count(Message.msg_hash))) == 2
