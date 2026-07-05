from httpx import ASGITransport, AsyncClient
import pytest

from app.database import get_db_session
from app.main import app


@pytest.mark.asyncio
async def test_message_ingest_api_accepts_wechat_normalized_message(db_session):
    async def override_db_session():
        yield db_session

    app.dependency_overrides[get_db_session] = override_db_session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            ingest_response = await client.post(
                "/api/messages",
                json={
                    "robot_id": "wxid_123",
                    "platform": "wechat",
                    "room_id": "wx_room_1",
                    "message_type": "private",
                    "sender_id": "wx_friend_1",
                    "nickname": "WeChat Friend",
                    "raw_message": "hello from wechat",
                    "timestamp": 1783000000,
                    "message_id": "wechat-msg-1",
                },
            )
            bots_response = await client.get("/api/bots")
            rooms_response = await client.get("/api/rooms", params={"robot_id": "wxid_123"})
            messages_response = await client.get(
                "/api/messages",
                params={"robot_id": "wxid_123", "room_id": "wx_room_1"},
            )
    finally:
        app.dependency_overrides.clear()

    assert ingest_response.status_code == 201
    assert len(ingest_response.json()["msg_hash"]) == 32
    assert bots_response.status_code == 200
    assert bots_response.json()[0]["id"] == "wxid_123"
    assert bots_response.json()[0]["platform"] == "wechat"
    assert rooms_response.status_code == 200
    assert rooms_response.json()[0]["room_id"] == "wx_room_1"
    assert rooms_response.json()[0]["message_type"] == "private"
    assert messages_response.status_code == 200
    assert messages_response.json()[0]["platform"] == "wechat"
    assert messages_response.json()[0]["external_message_id"] == "wechat-msg-1"
    assert messages_response.json()[0]["raw_message"] == "hello from wechat"
