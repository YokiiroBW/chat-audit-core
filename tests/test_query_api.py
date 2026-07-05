from httpx import ASGITransport, AsyncClient
import pytest

from app.database import get_db_session
from app.main import app
from app.models import Adapter, BotProfile, RoomProfile, UserProfile
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
    db_session.add(RoomProfile(room_id="group-shared", platform="qq", display_name="测试群", avatar_path="/static/storage/group.jpg"))
    db_session.add(UserProfile(user_id="user-1", platform="qq", display_name="Alice Local", avatar_path="/static/storage/user-1.jpg"))
    await db_session.commit()

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
    assert rooms_b_response.json()[0]["display_name"] == "测试群"
    assert rooms_b_response.json()[0]["avatar_path"] == "/static/storage/group.jpg"

    assert messages_b_response.status_code == 200
    messages_b = messages_b_response.json()
    assert len(messages_b) == 1
    assert messages_b[0]["room_id"] == "group-shared"
    assert messages_b[0]["raw_message"] == "shared message"
    assert messages_b[0]["sender_display_name"] == "Alice Local"
    assert messages_b[0]["sender_avatar_path"] == "/static/storage/user-1.jpg"

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
async def test_messages_api_includes_reply_preview_from_unloaded_message(db_session):
    await MessageService.process_incoming_message(
        db_session,
        "robot-a",
        "qq",
        {
            "message_id": "target-1",
            "room_id": "group-reply",
            "message_type": "group",
            "sender_id": "user-target",
            "nickname": "Target User",
            "raw_message": "original reply target",
            "timestamp": 1783000000,
        },
    )
    await MessageService.process_incoming_message(
        db_session,
        "robot-a",
        "qq",
        {
            "message_id": "reply-1",
            "room_id": "group-reply",
            "message_type": "group",
            "sender_id": "user-reply",
            "nickname": "Reply User",
            "raw_message": "[CQ:reply,id=target-1] reply body",
            "timestamp": 1783000010,
        },
    )

    async def override_db_session():
        yield db_session

    app.dependency_overrides[get_db_session] = override_db_session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(
                "/api/messages",
                params={"robot_id": "robot-a", "room_id": "group-reply", "limit": 1},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 1
    assert payload[0]["raw_message"] == "[CQ:reply,id=target-1] reply body"
    assert payload[0]["reply_to_message_id"] == "target-1"
    assert payload[0]["reply_preview_text"] == "Target User: original reply target"


@pytest.mark.asyncio
async def test_messages_api_can_load_context_around_reply_target(db_session):
    for index in range(5):
        await MessageService.process_incoming_message(
            db_session,
            "robot-a",
            "qq",
            {
                "message_id": f"msg-{index}",
                "room_id": "group-reply-jump",
                "message_type": "group",
                "sender_id": f"user-{index}",
                "nickname": f"User {index}",
                "raw_message": f"message {index}",
                "timestamp": 1783000000 + index,
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
                    "room_id": "group-reply-jump",
                    "around_message_id": "msg-2",
                    "limit": 3,
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert [item["external_message_id"] for item in payload] == ["msg-0", "msg-1", "msg-2"]


@pytest.mark.asyncio
async def test_private_room_api_hydrates_friend_avatar_and_preserves_name(db_session, monkeypatch):
    await MessageService.process_incoming_message(
        db_session,
        "robot-a",
        "qq",
        {
            "room_id": "123456789",
            "message_type": "private",
            "sender_id": "123456789",
            "nickname": "Private Friend",
            "raw_message": "hello private",
            "timestamp": 1783000000,
        },
    )

    async def fake_cache_qq_user_profile(
        db,
        *,
        user_id,
        platform,
        display_name,
        http_client,
        storage_root,
        public_prefix,
        max_bytes,
    ):
        await db.merge(
            UserProfile(
                user_id=user_id,
                platform=platform,
                display_name=display_name,
                avatar_path="/static/storage/private-friend.jpg",
            )
        )
        await db.commit()

    monkeypatch.setattr("app.api.UserProfileService.cache_qq_user_profile", fake_cache_qq_user_profile)

    async def override_db_session():
        yield db_session

    app.dependency_overrides[get_db_session] = override_db_session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/rooms", params={"robot_id": "robot-a"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload[0]["room_id"] == "123456789"
    assert payload[0]["message_type"] == "private"
    assert payload[0]["display_name"] == "Private Friend"
    assert payload[0]["avatar_path"] == "/static/storage/private-friend.jpg"


@pytest.mark.asyncio
async def test_private_room_api_refreshes_placeholder_svg_avatar(db_session, monkeypatch):
    await MessageService.process_incoming_message(
        db_session,
        "robot-a",
        "qq",
        {
            "room_id": "123456789",
            "message_type": "private",
            "sender_id": "123456789",
            "nickname": "Private Friend",
            "raw_message": "hello private",
            "timestamp": 1783000000,
        },
    )
    db_session.add(
        UserProfile(
            user_id="123456789",
            platform="qq",
            display_name="Private Friend",
            avatar_path="/static/storage/private-placeholder.svg",
        )
    )
    await db_session.commit()

    async def fake_cache_qq_user_profile(
        db,
        *,
        user_id,
        platform,
        display_name,
        http_client,
        storage_root,
        public_prefix,
        max_bytes,
    ):
        profile = await db.get(UserProfile, user_id)
        profile.avatar_path = "/static/storage/private-real.jpg"
        await db.commit()

    monkeypatch.setattr("app.api.UserProfileService.cache_qq_user_profile", fake_cache_qq_user_profile)

    async def override_db_session():
        yield db_session

    app.dependency_overrides[get_db_session] = override_db_session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/rooms", params={"robot_id": "robot-a"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()[0]["avatar_path"] == "/static/storage/private-real.jpg"


@pytest.mark.asyncio
async def test_messages_api_hydrates_missing_numeric_qq_sender_avatar(db_session, monkeypatch):
    await MessageService.process_incoming_message(
        db_session,
        "robot-a",
        "qq",
        {
            "room_id": "123456789",
            "message_type": "private",
            "sender_id": "123456789",
            "nickname": "Private Friend",
            "raw_message": "hello private",
            "timestamp": 1783000000,
        },
    )

    async def fake_cache_qq_user_profile(
        db,
        *,
        user_id,
        platform,
        display_name,
        http_client,
        storage_root,
        public_prefix,
        max_bytes,
    ):
        await db.merge(
            UserProfile(
                user_id=user_id,
                platform=platform,
                display_name=display_name,
                avatar_path="/static/storage/private-sender.jpg",
            )
        )
        await db.commit()

    monkeypatch.setattr("app.api.UserProfileService.cache_qq_user_profile", fake_cache_qq_user_profile)

    async def override_db_session():
        yield db_session

    app.dependency_overrides[get_db_session] = override_db_session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(
                "/api/messages",
                params={"robot_id": "robot-a", "room_id": "123456789"},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload[0]["sender_display_name"] == "Private Friend"
    assert payload[0]["sender_avatar_path"] == "/static/storage/private-sender.jpg"


@pytest.mark.asyncio
async def test_bots_api_lists_discovered_bot_profiles(db_session):
    db_session.add_all(
        [
            BotProfile(id="bot-a", platform="qq", status="gray", display_name="Bot A"),
            BotProfile(id="bot-b", platform="qq", status="green", source_adapter_id="adapter-a"),
            UserProfile(user_id="bot-a", platform="qq", display_name="Bot A", avatar_path="/static/storage/bot-a.jpg"),
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
    assert next(item for item in payload if item["id"] == "bot-a")["avatar_path"] == "/static/storage/bot-a.jpg"


@pytest.mark.asyncio
async def test_bots_api_hydrates_missing_numeric_qq_bot_avatar(db_session, monkeypatch):
    db_session.add(BotProfile(id="1449801200", platform="qq", status="green", display_name="NapCat2"))
    await db_session.commit()

    async def fake_cache_qq_user_profile(
        db,
        *,
        user_id,
        platform,
        display_name,
        http_client,
        storage_root,
        public_prefix,
        max_bytes,
    ):
        await db.merge(
            UserProfile(
                user_id=user_id,
                platform=platform,
                display_name=display_name,
                avatar_path="/static/storage/bot-avatar.jpg",
            )
        )
        await db.commit()

    monkeypatch.setattr("app.api.UserProfileService.cache_qq_user_profile", fake_cache_qq_user_profile)

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
    assert payload[0]["id"] == "1449801200"
    assert payload[0]["avatar_path"] == "/static/storage/bot-avatar.jpg"


@pytest.mark.asyncio
async def test_bots_api_refreshes_placeholder_svg_avatar(db_session, monkeypatch):
    db_session.add(BotProfile(id="1449801200", platform="qq", status="green", display_name="NapCat2"))
    db_session.add(
        UserProfile(
            user_id="1449801200",
            platform="qq",
            display_name="NapCat2",
            avatar_path="/static/storage/bot-placeholder.svg",
        )
    )
    await db_session.commit()

    async def fake_cache_qq_user_profile(
        db,
        *,
        user_id,
        platform,
        display_name,
        http_client,
        storage_root,
        public_prefix,
        max_bytes,
    ):
        profile = await db.get(UserProfile, user_id)
        profile.avatar_path = "/static/storage/bot-real.jpg"
        await db.commit()

    monkeypatch.setattr("app.api.UserProfileService.cache_qq_user_profile", fake_cache_qq_user_profile)

    async def override_db_session():
        yield db_session

    app.dependency_overrides[get_db_session] = override_db_session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/bots")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()[0]["avatar_path"] == "/static/storage/bot-real.jpg"
