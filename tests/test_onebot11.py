from fastapi.testclient import TestClient
import pytest
from starlette.websockets import WebSocketDisconnect

from app.database import get_db_session
from app.main import app
from app.services.query_service import QueryService


class StubAsyncClient:
    def __init__(self, payloads: dict[str, bytes]):
        self.payloads = payloads
        self.requested_urls: list[str] = []

    async def get(self, url: str):
        import httpx

        self.requested_urls.append(url)
        return httpx.Response(200, content=self.payloads[url])


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


@pytest.mark.asyncio
async def test_onebot_websocket_downloads_cq_media_and_static_route_serves_it(db_session):
    from app.ws import get_media_http_client

    media_bytes = b"ws image bytes unique"
    stub_client = StubAsyncClient({"http://media.local/ws-image.jpg": media_bytes})

    async def override_db_session():
        yield db_session

    async def override_media_client():
        yield stub_client

    event = {
        "post_type": "message",
        "message_type": "group",
        "self_id": 123456,
        "group_id": 998877,
        "user_id": 445566,
        "raw_message": "[CQ:image,file=abc.image,url=http://media.local/ws-image.jpg]",
        "time": 1783000400,
        "sender": {"nickname": "Alice"},
    }

    app.dependency_overrides[get_db_session] = override_db_session
    app.dependency_overrides[get_media_http_client] = override_media_client
    try:
        with TestClient(app) as client:
            with client.websocket_connect("/onebot/v11/ws") as websocket:
                websocket.send_json(event)
                ack = websocket.receive_json()

            messages = await QueryService.list_messages(db_session, robot_id="123456", room_id="998877")
            static_response = client.get(messages[0].local_message)
    finally:
        app.dependency_overrides.clear()

    assert ack["status"] == "stored"
    assert stub_client.requested_urls == ["http://media.local/ws-image.jpg"]
    assert len(messages) == 1
    assert messages[0].local_message.startswith("/static/storage/")
    assert "http://media.local" not in messages[0].local_message
    assert static_response.status_code == 200
    assert static_response.content == media_bytes


def test_onebot_websocket_rejects_missing_or_invalid_access_token_when_configured():
    from app.ws import get_onebot_access_token

    def configured_token():
        return "secret-token"

    app.dependency_overrides[get_onebot_access_token] = configured_token
    try:
        with TestClient(app) as client:
            with pytest.raises(WebSocketDisconnect) as missing_exc:
                with client.websocket_connect("/onebot/v11/ws"):
                    pass
            with pytest.raises(WebSocketDisconnect) as invalid_exc:
                with client.websocket_connect("/onebot/v11/ws?access_token=wrong"):
                    pass
            with client.websocket_connect("/onebot/v11/ws?access_token=secret-token") as websocket:
                websocket.send_json({"post_type": "meta_event"})
                ack = websocket.receive_json()
    finally:
        app.dependency_overrides.clear()

    assert missing_exc.value.code == 1008
    assert invalid_exc.value.code == 1008
    assert ack == {"status": "ignored"}


def test_onebot_websocket_accepts_bearer_authorization_header_when_configured():
    from app.ws import get_onebot_access_token

    def configured_token():
        return "secret-token"

    app.dependency_overrides[get_onebot_access_token] = configured_token
    try:
        with TestClient(app) as client:
            with client.websocket_connect(
                "/onebot/v11/ws",
                headers={"Authorization": "Bearer secret-token"},
            ) as websocket:
                websocket.send_json({"post_type": "meta_event"})
                ack = websocket.receive_json()
    finally:
        app.dependency_overrides.clear()

    assert ack == {"status": "ignored"}
