from httpx import ASGITransport, AsyncClient
import json
from pathlib import Path

import pytest

from app.adapters.wechat_pc import normalize_wechat_event
from app.config import Settings, get_settings
from app.database import get_db_session
from app.main import app
from app.services.message_service import MessageService


FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


class StubAsyncClient:
    def __init__(self, payloads: dict[str, bytes]):
        self.payloads = payloads
        self.requested_urls: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def get(self, url: str):
        import httpx

        self.requested_urls.append(url)
        return httpx.Response(200, content=self.payloads[url])


def test_normalize_wechat_group_text_event_accepts_common_hook_fields():
    event = {
        "event": "message",
        "robot_id": "wxid_bot",
        "room_id": "12345@chatroom",
        "sender_wxid": "wxid_friend",
        "sender_name": "微信好友",
        "content": "hello wechat",
        "timestamp": 1783100000,
        "msg_id": "wx-msg-1",
    }

    normalized = normalize_wechat_event(event)

    assert normalized is not None
    assert normalized.robot_id == "wxid_bot"
    assert normalized.platform == "wechat"
    assert normalized.msg_data == {
        "room_id": "12345@chatroom",
        "message_type": "group",
        "sender_id": "wxid_friend",
        "nickname": "微信好友",
        "raw_message": "hello wechat",
        "local_message": "hello wechat",
        "timestamp": 1783100000,
        "message_id": "wx-msg-1",
    }


def test_normalize_wechat_image_event_builds_cq_media_segment():
    event = {
        "event": "message",
        "self_id": "wxid_bot",
        "talker": "wxid_friend",
        "sender_wxid": "wxid_friend",
        "msg_type": "image",
        "media_url": "http://media.local/wechat.jpg",
        "timestamp": 1783100001,
    }

    normalized = normalize_wechat_event(event)

    assert normalized is not None
    assert normalized.msg_data["message_type"] == "private"
    assert normalized.msg_data["raw_message"] == "[CQ:image,file=wechat.jpg,url=http://media.local/wechat.jpg]"


def test_normalize_wechat_nested_numeric_image_event():
    event = {
        "type_name": "recv_msg",
        "data": {
            "CurrentWxid": "wxid_bot",
            "FromUserName": "12345@chatroom",
            "SenderWxid": "wxid_sender",
            "MsgType": 3,
            "FileName": "nested-image.dat",
            "FileUrl": "http://media.local/nested.jpg",
            "CreateTime": 1783100100,
            "MsgId": "wx-nested-image",
        },
    }

    normalized = normalize_wechat_event(event)

    assert normalized is not None
    assert normalized.robot_id == "wxid_bot"
    assert normalized.msg_data["room_id"] == "12345@chatroom"
    assert normalized.msg_data["message_type"] == "group"
    assert normalized.msg_data["sender_id"] == "wxid_sender"
    assert normalized.msg_data["raw_message"] == "[CQ:image,file=nested-image.dat,url=http://media.local/nested.jpg]"
    assert normalized.msg_data["message_id"] == "wx-nested-image"


def test_normalize_wechat_group_text_strips_sender_prefix():
    event = {
        "event": "message",
        "current_wxid": "wxid_bot",
        "talker": "12345@chatroom",
        "Content": "wxid_real_sender:\n群文本",
        "CreateTime": 1783100101,
    }

    normalized = normalize_wechat_event(event)

    assert normalized is not None
    assert normalized.msg_data["sender_id"] == "wxid_real_sender"
    assert normalized.msg_data["raw_message"] == "群文本"


@pytest.mark.parametrize("sample", json.loads((FIXTURES_DIR / "wechat_hook_samples.json").read_text(encoding="utf-8")))
def test_normalize_wechat_fixture_samples(sample):
    normalized = normalize_wechat_event(sample["event"])

    assert normalized is not None, sample["name"]
    expected = sample["expected"]
    assert normalized.robot_id == expected["robot_id"]
    assert normalized.platform == "wechat"
    assert normalized.msg_data["room_id"] == expected["room_id"]
    assert normalized.msg_data["message_type"] == expected["message_type"]
    assert normalized.msg_data["sender_id"] == expected["sender_id"]
    if "raw_message_prefix" in expected:
        assert normalized.msg_data["raw_message"].startswith(expected["raw_message_prefix"])
    else:
        assert normalized.msg_data["raw_message"] == expected["raw_message"]
    assert normalized.msg_data["local_message"] == normalized.msg_data["raw_message"]
    assert normalized.msg_data["message_id"] == expected["message_id"]


@pytest.mark.asyncio
async def test_wechat_event_api_ingests_text_message(db_session, tmp_path):
    async def override_db_session():
        yield db_session

    def override_settings():
        return Settings(storage_root=tmp_path, public_storage_prefix="/static/storage")

    app.dependency_overrides[get_db_session] = override_db_session
    app.dependency_overrides[get_settings] = override_settings
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            ingest_response = await client.post(
                "/api/wechat/events",
                json={
                    "event": "message",
                    "robot_id": "wxid_bot",
                    "room_id": "wxid_friend",
                    "sender_wxid": "wxid_friend",
                    "sender_name": "微信好友",
                    "content": "hello from hook",
                    "timestamp": 1783100002,
                    "msg_id": "wx-hook-1",
                },
            )
            messages_response = await client.get(
                "/api/messages",
                params={"robot_id": "wxid_bot", "room_id": "wxid_friend"},
            )
    finally:
        app.dependency_overrides.clear()

    assert ingest_response.status_code == 201
    assert messages_response.status_code == 200
    message = messages_response.json()[0]
    assert message["platform"] == "wechat"
    assert message["external_message_id"] == "wx-hook-1"
    assert message["raw_message"] == "hello from hook"
    assert message["sender_display_name"] == "微信好友"
    assert message["sender_avatar_path"].startswith("/static/storage/")


@pytest.mark.asyncio
async def test_wechat_event_api_localizes_image_message(db_session, tmp_path, monkeypatch):
    async def override_db_session():
        yield db_session

    def override_settings():
        return Settings(storage_root=tmp_path, public_storage_prefix="/static/storage")

    stub_client = StubAsyncClient({"http://media.local/wechat.jpg": b"wechat image"})
    monkeypatch.setattr("app.api.httpx.AsyncClient", lambda timeout=None: stub_client)
    app.dependency_overrides[get_db_session] = override_db_session
    app.dependency_overrides[get_settings] = override_settings
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/api/wechat/events",
                json={
                    "event": "message",
                    "robot_id": "wxid_bot",
                    "room_id": "wxid_friend",
                    "sender_wxid": "wxid_friend",
                    "msg_type": "image",
                    "media_url": "http://media.local/wechat.jpg",
                    "timestamp": 1783100003,
                    "msg_id": "wx-image-1",
                },
            )
    finally:
        app.dependency_overrides.clear()

    assets = await MessageService.list_media_assets(db_session)
    messages = await MessageService.list_messages(db_session)

    assert response.status_code == 201
    assert stub_client.requested_urls == ["http://media.local/wechat.jpg"]
    assert len(assets) == 2
    assert messages[0].platform == "wechat"
    assert messages[0].local_message in {asset.local_path for asset in assets}
