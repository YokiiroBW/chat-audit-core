from httpx import ASGITransport, AsyncClient
import pytest

from app.database import get_db_session
from app.main import app
from app.models import Adapter, BotProfile
from app.services.message_service import MessageService


@pytest.mark.asyncio
async def test_query_api_respects_robot_view_isolation(db_session):
    db_session.add_all(
        [
            Adapter(id="robot-a", platform="qq", status="green"),
            Adapter(id="robot-b", platform="qq", status="gray"),
        ]
    )
    await db_session.commit()

    shared_payload = {
        "room_id": "group-shared",
        "message_type": "group",
        "sender_id": "user-1",
        "nickname": "Alice",
        "raw_message": "shared message",
        "timestamp": 1783000000,
    }
    robot_a_only_payload = {
        "room_id": "group-a-only",
        "message_type": "group",
        "sender_id": "user-2",
        "nickname": "Bob",
        "raw_message": "only robot a can see this",
        "timestamp": 1783000010,
    }

    await MessageService.process_incoming_message(db_session, "robot-a", "qq", shared_payload)
    await MessageService.process_incoming_message(db_session, "robot-b", "qq", shared_payload)
    await MessageService.process_incoming_message(db_session, "robot-a", "qq", robot_a_only_payload)

    async def override_db_session():
        yield db_session

    app.dependency_overrides[get_db_session] = override_db_session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            adapters_response = await client.get("/api/adapters")
            rooms_a_response = await client.get("/api/rooms", params={"robot_id": "robot-a"})
            rooms_b_response = await client.get("/api/rooms", params={"robot_id": "robot-b"})
            messages_b_response = await client.get(
                "/api/messages",
                params={"robot_id": "robot-b", "room_id": "group-shared", "limit": 50},
            )
            hidden_messages_response = await client.get(
                "/api/messages",
                params={"robot_id": "robot-b", "room_id": "group-a-only", "limit": 50},
            )
    finally:
        app.dependency_overrides.clear()

    assert adapters_response.status_code == 200
    assert [item["id"] for item in adapters_response.json()] == ["robot-a", "robot-b"]

    assert rooms_a_response.status_code == 200
    assert {item["room_id"] for item in rooms_a_response.json()} == {"group-shared", "group-a-only"}

    assert rooms_b_response.status_code == 200
    assert [item["room_id"] for item in rooms_b_response.json()] == ["group-shared"]

    assert messages_b_response.status_code == 200
    messages_b = messages_b_response.json()
    assert len(messages_b) == 1
    assert messages_b[0]["room_id"] == "group-shared"
    assert messages_b[0]["raw_message"] == "shared message"

    assert hidden_messages_response.status_code == 200
    assert hidden_messages_response.json() == []


@pytest.mark.asyncio
async def test_messages_api_uses_before_timestamp_cursor(db_session):
    for offset in range(3):
        await MessageService.process_incoming_message(
            db_session,
            "robot-a",
            "qq",
            {
                "room_id": "group-cursor",
                "message_type": "group",
                "sender_id": f"user-{offset}",
                "nickname": f"User {offset}",
                "raw_message": f"message {offset}",
                "timestamp": 1783000000 + offset,
            },
        )

    async def override_db_session():
        yield db_session

    app.dependency_overrides[get_db_session] = override_db_session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(
                "/api/messages",
                params={
                    "robot_id": "robot-a",
                    "room_id": "group-cursor",
                    "before_timestamp": 1783000002,
                    "limit": 2,
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert [item["raw_message"] for item in payload] == ["message 0", "message 1"]


@pytest.mark.asyncio
async def test_bots_api_lists_discovered_bot_profiles(db_session):
    db_session.add_all(
        [
            BotProfile(id="bot-a", platform="qq", status="gray", display_name="Bot A"),
            BotProfile(id="bot-b", platform="qq", status="green", source_adapter_id="adapter-a"),
        ]
    )
    await db_session.commit()

    async def override_db_session():
        yield db_session

    app.dependency_overrides[get_db_session] = override_db_session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/bots")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert {item["id"] for item in payload} == {"bot-a", "bot-b"}
    assert next(item for item in payload if item["id"] == "bot-a")["display_name"] == "Bot A"
