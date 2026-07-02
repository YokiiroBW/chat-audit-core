from fastapi.testclient import TestClient
import pytest

from app.database import get_db_session
from app.main import app
from app.services.query_service import QueryService


def test_normalize_group_message_event_maps_onebot_fields():
    from app.adapters.onebot11 import normalize_message_event

    event = {
        "post_type": "message",
        "message_type": "group",
        "self_id": 123456,
        "group_id": 998877,
        "user_id": 445566,
        "raw_message": "hello from napcat",
        "time": 1783000100,
        "sender": {"card": "群名片", "nickname": "昵称"},
    }

    normalized = normalize_message_event(event)

    assert normalized.robot_id == "123456"
    assert normalized.platform == "qq"
    assert normalized.msg_data == {
        "room_id": "998877",
        "message_type": "group",
        "sender_id": "445566",
        "nickname": "群名片",
        "raw_message": "hello from napcat",
        "local_message": "hello from napcat",
        "timestamp": 1783000100,
    }


@pytest.mark.asyncio
async def test_onebot_websocket_persists_group_message_with_robot_view(db_session):
    async def override_db_session():
        yield db_session

    event = {
        "post_type": "message",
        "message_type": "group",
        "self_id": 123456,
        "group_id": 998877,
        "user_id": 445566,
        "raw_message": "websocket hello",
        "time": 1783000200,
        "sender": {"nickname": "Alice"},
    }

    app.dependency_overrides[get_db_session] = override_db_session
    try:
        with TestClient(app) as client:
            with client.websocket_connect("/onebot/v11/ws") as websocket:
                websocket.send_json(event)
                ack = websocket.receive_json()
    finally:
        app.dependency_overrides.clear()

    assert ack["status"] == "stored"
    assert len(ack["msg_hash"]) == 32

    rooms = await QueryService.list_rooms(db_session, robot_id="123456")
    messages = await QueryService.list_messages(db_session, robot_id="123456", room_id="998877")

    assert rooms == [{"room_id": "998877", "last_timestamp": 1783000200}]
    assert len(messages) == 1
    assert messages[0].raw_message == "websocket hello"
