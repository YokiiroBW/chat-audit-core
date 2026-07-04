import pytest

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
