from httpx import ASGITransport, AsyncClient
import json
import pytest
from sqlalchemy import select

from app.config import Settings, get_settings
from app.database import get_db_session
from app.main import app


@pytest.mark.asyncio
async def test_message_ingest_api_accepts_custom_normalized_message(db_session):
    async def override_db_session():
        yield db_session

    app.dependency_overrides[get_db_session] = override_db_session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            ingest_response = await client.post(
                "/api/messages",
                json={
                    "robot_id": "custom-bot-1",
                    "platform": "custom",
                    "room_id": "custom-room-1",
                    "message_type": "private",
                    "sender_id": "custom-user-1",
                    "nickname": "Custom Friend",
                    "raw_message": "hello from custom adapter",
                    "timestamp": 1783000000,
                    "message_id": "custom-msg-1",
                },
            )
            bots_response = await client.get("/api/bots")
            rooms_response = await client.get("/api/rooms", params={"robot_id": "custom-bot-1"})
            messages_response = await client.get(
                "/api/messages",
                params={"robot_id": "custom-bot-1", "room_id": "custom-room-1"},
            )
    finally:
        app.dependency_overrides.clear()

    assert ingest_response.status_code == 201
    assert len(ingest_response.json()["msg_hash"]) == 32
    assert bots_response.status_code == 200
    assert bots_response.json()[0]["id"] == "custom-bot-1"
    assert bots_response.json()[0]["platform"] == "custom"
    assert rooms_response.status_code == 200
    assert rooms_response.json()[0]["room_id"] == "custom-room-1"
    assert rooms_response.json()[0]["message_type"] == "private"
    assert messages_response.status_code == 200
    assert messages_response.json()[0]["platform"] == "custom"
    assert messages_response.json()[0]["external_message_id"] == "custom-msg-1"
    assert messages_response.json()[0]["raw_message"] == "hello from custom adapter"


@pytest.mark.asyncio
async def test_external_media_upload_saves_file_and_requires_operator_token(db_session, tmp_path):
    settings = Settings(
        admin_api_token="",
        admin_api_tokens=json.dumps([{"name": "ops", "role": "operator", "token": "operator-token"}]),
        storage_root=tmp_path,
        public_storage_prefix="/static/storage",
    )

    async def override_db_session():
        yield db_session

    app.dependency_overrides[get_db_session] = override_db_session
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            missing_token = await client.post(
                "/api/external/media",
                files={
                    "media_type": (None, "image"),
                    "file": ("custom.jpg", b"custom image", "image/jpeg"),
                },
            )
            uploaded = await client.post(
                "/api/external/media",
                headers={"Authorization": "Bearer operator-token"},
                files={
                    "media_type": (None, "image"),
                    "file": ("custom.jpg", b"custom image", "image/jpeg"),
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert missing_token.status_code == 401
    assert uploaded.status_code == 201
    body = uploaded.json()
    assert body["media_type"] == "image"
    assert body["file_name"] == "custom.jpg"
    assert body["file_size"] == len(b"custom image")
    assert body["local_path"].startswith("/static/storage/")
    assert (tmp_path / body["local_path"].rsplit("/", 1)[-1]).read_bytes() == b"custom image"


@pytest.mark.asyncio
async def test_capture_policy_api_can_skip_blacklisted_target(db_session):
    async def override_db_session():
        yield db_session

    app.dependency_overrides[get_db_session] = override_db_session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            policy_response = await client.put(
                "/api/bots/robot-a/capture-policies/group/955973452",
                json={
                    "list_mode": "blacklist",
                    "capture_text": True,
                    "capture_image": True,
                    "capture_voice": True,
                    "capture_video": True,
                    "capture_file": False,
                },
            )
            ingest_response = await client.post(
                "/api/messages",
                json={
                    "robot_id": "robot-a",
                    "platform": "qq",
                    "room_id": "955973452",
                    "message_type": "group",
                    "sender_id": "user-1",
                    "nickname": "Alice",
                    "raw_message": "blocked",
                    "timestamp": 1783000000,
                    "message_id": "blocked-msg-1",
                },
            )
            messages_response = await client.get(
                "/api/messages",
                params={"robot_id": "robot-a", "room_id": "955973452"},
            )
            targets_response = await client.get("/api/bots/robot-a/capture-targets")
            delete_response = await client.delete("/api/bots/robot-a/capture-policies/group/955973452")
    finally:
        app.dependency_overrides.clear()

    assert policy_response.status_code == 200
    assert policy_response.json()["list_mode"] == "blacklist"
    assert ingest_response.status_code == 201
    assert ingest_response.json()["msg_hash"] is None
    assert ingest_response.json()["skipped"] is True
    assert messages_response.status_code == 200
    assert messages_response.json() == []
    assert targets_response.status_code == 200
    assert targets_response.json()[0]["policy"]["list_mode"] == "blacklist"
    assert delete_response.status_code == 204


@pytest.mark.asyncio
async def test_ingest_keys_on_canonical_identity_and_records_the_alias(db_session):
    # The endpoint accepted canonical ids and the importer keys on them, but
    # this path dropped them, so the same private conversation arriving under
    # two identity variants split into two rooms.
    from app.models import IdentityAlias, Message
    from app.services.message_service import MessageService

    first = await MessageService.process_incoming_message(
        db_session,
        "robot-a",
        "qq",
        {
            "message_id": "canon-1",
            "room_id": "u_AbCdEf-raw-uid",
            "message_type": "private",
            "sender_id": "u_AbCdEf-raw-uid",
            "canonical_room_id": "700100",
            "canonical_sender_id": "700100",
            "nickname": "Peer",
            "raw_message": "first",
            "timestamp": 1783000000,
        },
    )
    # Same conversation, this time already reported under the canonical id.
    await MessageService.process_incoming_message(
        db_session,
        "robot-a",
        "qq",
        {
            "message_id": "canon-2",
            "room_id": "700100",
            "message_type": "private",
            "sender_id": "700100",
            "nickname": "Peer",
            "raw_message": "second",
            "timestamp": 1783000001,
        },
    )

    rooms = set((await db_session.execute(select(Message.room_id))).scalars().all())
    assert rooms == {"700100"}
    stored = await db_session.get(Message, first)
    assert stored.sender_id == "700100"

    aliases = list((await db_session.execute(select(IdentityAlias))).scalars().all())
    assert {(alias.identity_type, alias.alias_id, alias.canonical_id) for alias in aliases} == {
        ("user", "u_AbCdEf-raw-uid", "700100"),
        ("conversation", "u_AbCdEf-raw-uid", "700100"),
    }
