from httpx import ASGITransport, AsyncClient
import pytest

from app.database import get_db_session
from app.main import app
from app.services.message_service import MessageService


@pytest.mark.asyncio
async def test_search_api_filters_keyword_sender_room_time_and_robot_view(db_session):
    messages = [
        (
            "robot-a",
            {
                "room_id": "room-1",
                "message_type": "group",
                "sender_id": "alice",
                "nickname": "Alice",
                "raw_message": "alpha keyword target",
                "timestamp": 100,
            },
        ),
        (
            "robot-a",
            {
                "room_id": "room-1",
                "message_type": "group",
                "sender_id": "bob",
                "nickname": "Bob",
                "raw_message": "alpha keyword from wrong sender",
                "timestamp": 110,
            },
        ),
        (
            "robot-a",
            {
                "room_id": "room-2",
                "message_type": "group",
                "sender_id": "alice",
                "nickname": "Alice",
                "raw_message": "alpha keyword wrong room",
                "timestamp": 120,
            },
        ),
        (
            "robot-b",
            {
                "room_id": "room-1",
                "message_type": "group",
                "sender_id": "alice",
                "nickname": "Alice",
                "raw_message": "alpha keyword hidden from robot a",
                "timestamp": 130,
            },
        ),
    ]
    for robot_id, payload in messages:
        await MessageService.process_incoming_message(db_session, robot_id, "qq", payload)

    async def override_db_session():
        yield db_session

    app.dependency_overrides[get_db_session] = override_db_session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(
                "/api/search",
                params={
                    "robot_id": "robot-a",
                    "keyword": "target",
                    "room_id": "room-1",
                    "sender_id": "alice",
                    "start_timestamp": 90,
                    "end_timestamp": 105,
                    "limit": 20,
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 1
    assert payload[0]["room_id"] == "room-1"
    assert payload[0]["sender_id"] == "alice"
    assert payload[0]["raw_message"] == "alpha keyword target"


@pytest.mark.asyncio
async def test_search_api_requires_robot_id_for_view_isolation(db_session):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/search", params={"keyword": "anything"})

    assert response.status_code == 422
