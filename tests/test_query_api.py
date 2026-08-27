from httpx import ASGITransport, AsyncClient
import pytest

from app.database import get_db_session
from app.main import app
from app.models import Adapter, BotProfile, ImportSource, Message, MessageSourceRecord, RoomProfile, UserProfile
from app.services.message_service import MessageService
from app.services.query_service import QueryService


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
async def test_room_id_collision_requires_and_respects_message_type_across_flows(db_session):
    for message_type, target_body, reply_id in (
        ("group", "group target body", "group-reply"),
        ("private", "private target body", "private-reply"),
    ):
        await MessageService.process_incoming_message(
            db_session,
            "robot-a",
            "qq",
            {
                "message_id": "shared-target-id",
                "room_id": "shared-room-id",
                "message_type": message_type,
                "sender_id": "shared-sender",
                "nickname": f"{message_type.title()} Target",
                "raw_message": target_body,
                "timestamp": 1783000000,
            },
        )
        await MessageService.process_incoming_message(
            db_session,
            "robot-a",
            "qq",
            {
                "message_id": reply_id,
                "room_id": "shared-room-id",
                "message_type": message_type,
                "sender_id": "shared-sender",
                "nickname": f"{message_type.title()} Replier",
                "raw_message": "[CQ:reply,id=shared-target-id] follow up",
                "timestamp": 1783000001,
            },
        )
    db_session.add_all(
        [
            RoomProfile(room_id="shared-room-id", platform="qq", display_name="Group Profile"),
            UserProfile(user_id="shared-room-id", platform="qq", display_name="Private Profile"),
        ]
    )
    await db_session.commit()

    async def override_db_session():
        yield db_session

    app.dependency_overrides[get_db_session] = override_db_session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            rooms = await client.get("/api/rooms", params={"robot_id": "robot-a"})
            ambiguous_messages = await client.get(
                "/api/messages", params={"robot_id": "robot-a", "room_id": "shared-room-id"}
            )
            group_messages = await client.get(
                "/api/messages",
                params={"robot_id": "robot-a", "room_id": "shared-room-id", "message_type": "group"},
            )
            private_messages = await client.get(
                "/api/messages",
                params={"robot_id": "robot-a", "room_id": "shared-room-id", "message_type": "private"},
            )
            ambiguous_search = await client.get(
                "/api/search",
                params={"robot_id": "robot-a", "room_id": "shared-room-id", "keyword": "target"},
            )
            group_search = await client.get(
                "/api/search",
                params={
                    "robot_id": "robot-a",
                    "room_id": "shared-room-id",
                    "message_type": "group",
                    "keyword": "target",
                },
            )
            ambiguous_export = await client.get(
                "/api/export", params={"robot_id": "robot-a", "room_id": "shared-room-id"}
            )
            private_export = await client.get(
                "/api/export",
                params={"robot_id": "robot-a", "room_id": "shared-room-id", "message_type": "private"},
            )
    finally:
        app.dependency_overrides.clear()

    assert rooms.status_code == 200
    room_items = {(item["room_id"], item["message_type"]): item for item in rooms.json()}
    assert room_items[("shared-room-id", "group")]["display_name"] == "Group Profile"
    assert room_items[("shared-room-id", "private")]["display_name"] == "Private Profile"
    assert ambiguous_messages.status_code == 409
    assert ambiguous_search.status_code == 409
    assert ambiguous_export.status_code == 409
    assert {item["message_type"] for item in group_messages.json()} == {"group"}
    assert {item["message_type"] for item in private_messages.json()} == {"private"}
    group_reply = next(item for item in group_messages.json() if item["external_message_id"] == "group-reply")
    private_reply = next(item for item in private_messages.json() if item["external_message_id"] == "private-reply")
    assert group_reply["reply_preview_text"] == "Group Target: group target body"
    assert private_reply["reply_preview_text"] == "Private Target: private target body"
    assert {item["message_type"] for item in group_search.json()} == {"group"}
    assert {item["message_type"] for item in private_export.json()["messages"]} == {"private"}


@pytest.mark.asyncio
async def test_around_message_cursor_includes_target_among_many_same_second_messages(db_session):
    target_id = "same-second-target"
    for index in range(230):
        await MessageService.process_incoming_message(
            db_session,
            "robot-a",
            "qq",
            {
                "message_id": target_id if index == 5 else f"same-second-{index}",
                "room_id": "group-around-dense",
                "message_type": "group",
                "sender_id": "dense-sender",
                "nickname": "Dense Sender",
                "raw_message": f"dense message {index}",
                "timestamp": 1783000200,
                "source_sequence": index,
            },
        )

    messages = await QueryService.list_messages(
        db_session,
        robot_id="robot-a",
        room_id="group-around-dense",
        message_type="group",
        around_message_id=target_id,
        limit=50,
    )

    assert target_id in {message.external_message_id for message in messages}


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
async def test_rooms_api_keeps_all_rooms_when_avatar_hydration_fails(db_session, monkeypatch):
    for room_id, nickname in (("123456789", "Private Friend"), ("987654321", "Another Friend")):
        await MessageService.process_incoming_message(
            db_session,
            "robot-a",
            "qq",
            {
                "room_id": room_id,
                "message_type": "private",
                "sender_id": room_id,
                "nickname": nickname,
                "raw_message": "hello private",
                "timestamp": 1783000000,
            },
        )

    async def failing_cache_qq_user_profile(**kwargs):
        raise RuntimeError("avatar asset is not readable")

    monkeypatch.setattr("app.api.UserProfileService.cache_qq_user_profile", failing_cache_qq_user_profile)

    async def override_db_session():
        yield db_session

    app.dependency_overrides[get_db_session] = override_db_session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/rooms", params={"robot_id": "robot-a"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert {item["room_id"] for item in response.json()} == {"123456789", "987654321"}


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


@pytest.mark.asyncio
async def test_messages_api_composite_cursor_keeps_same_timestamp_rows(db_session):
    for offset in range(3):
        await MessageService.process_incoming_message(
            db_session,
            "robot-a",
            "qq",
            {
                "room_id": "group-composite-cursor",
                "message_type": "group",
                "sender_id": f"user-{offset}",
                "nickname": f"User {offset}",
                "raw_message": f"same-second {offset}",
                "timestamp": 1783000200,
                "source_sequence": offset,
            },
        )

    async def override_db_session():
        yield db_session

    app.dependency_overrides[get_db_session] = override_db_session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            newest = await client.get("/api/messages", params={"robot_id": "robot-a", "room_id": "group-composite-cursor", "limit": 1})
            cursor = newest.json()[0]
            older = await client.get(
                "/api/messages",
                params={
                    "robot_id": "robot-a",
                    "room_id": "group-composite-cursor",
                    "before_timestamp": cursor["timestamp"],
                    "before_source_sequence": cursor["source_sequence"] if cursor["source_sequence"] is not None else -1,
                    "before_msg_hash": cursor["msg_hash"],
                    "limit": 10,
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert newest.status_code == 200
    assert older.status_code == 200
    assert {item["raw_message"] for item in older.json()} == {"same-second 0", "same-second 1"}


@pytest.mark.asyncio
async def test_query_includes_imported_messages_when_robot_view_link_is_missing(db_session):
    source = ImportSource(
        id="qqnt-source-account-a",
        source_type="qqnt_local_db",
        platform="qq",
        account_id="robot-a",
        device_id="device-a",
        status="active",
    )
    message = Message(
        msg_hash="imported-only-message-hash",
        platform="qq",
        room_id="friend-9001",
        message_type="private",
        external_message_id="imported-1",
        sender_id="9001",
        nickname="Imported Friend",
        raw_message="historical imported message",
        local_message="historical imported message",
        timestamp=1783000100,
    )
    source_record = MessageSourceRecord(
        msg_hash=message.msg_hash,
        source_id=source.id,
        source_table="messages",
        source_primary_key="imported-1",
        source_external_message_id="imported-1",
        raw_columns_json='{"40030":"9001"}',
    )
    db_session.add_all([source, message, source_record])
    await db_session.commit()

    rooms = await QueryService.list_rooms(db_session, robot_id="robot-a")
    messages = await QueryService.list_messages(
        db_session,
        robot_id="robot-a",
        room_id="friend-9001",
        limit=50,
    )

    assert [room["room_id"] for room in rooms] == ["friend-9001"]
    assert [item.raw_message for item in messages] == ["historical imported message"]


@pytest.mark.asyncio
async def test_reply_preview_never_resolves_to_another_accounts_message(db_session):
    # OneBot message ids are small per-connection integers, so two accounts
    # reusing "1001" is the normal case, not an edge case. Resolving the reply
    # target globally leaked the other account's sender and body.
    await MessageService.process_incoming_message(
        db_session,
        "robot-a",
        "qq",
        {
            "message_id": "1001",
            "room_id": "group-a",
            "message_type": "group",
            "sender_id": "user-a",
            "nickname": "A Target",
            "raw_message": "robot a target body",
            "timestamp": 1783000000,
        },
    )
    # Inserted after robot-a's row so that an unscoped lookup would pick this
    # one: the old code kept whichever row the database happened to emit last.
    await MessageService.process_incoming_message(
        db_session,
        "robot-b",
        "qq",
        {
            "message_id": "1001",
            "room_id": "group-b",
            "message_type": "group",
            "sender_id": "user-b",
            "nickname": "B Secret",
            "raw_message": "robot b confidential body",
            "timestamp": 1783000005,
        },
    )
    await MessageService.process_incoming_message(
        db_session,
        "robot-a",
        "qq",
        {
            "message_id": "reply-a",
            "room_id": "group-a",
            "message_type": "group",
            "sender_id": "user-a2",
            "nickname": "A Replier",
            "raw_message": "[CQ:reply,id=1001] agreed",
            "timestamp": 1783000010,
        },
    )

    messages = await QueryService.list_messages(
        db_session,
        robot_id="robot-a",
        room_id="group-a",
        limit=50,
    )

    reply = next(item for item in messages if item.external_message_id == "reply-a")
    assert reply.reply_to_message_id == "1001"
    assert reply.reply_preview_text == "A Target: robot a target body"
    assert "confidential" not in (reply.reply_preview_text or "")
    assert "B Secret" not in (reply.reply_preview_text or "")


@pytest.mark.asyncio
async def test_reply_preview_stays_inside_its_own_room_for_one_account(db_session):
    # The robot scope alone is not enough: one account seeing two rooms that
    # both carry message id "2002" still needs the room to disambiguate.
    for room_id, nickname, body, timestamp in (
        ("group-x", "X Target", "room x target body", 1783000000),
        ("group-y", "Y Target", "room y target body", 1783000005),
    ):
        await MessageService.process_incoming_message(
            db_session,
            "robot-a",
            "qq",
            {
                "message_id": "2002",
                "room_id": room_id,
                "message_type": "group",
                "sender_id": f"user-{room_id}",
                "nickname": nickname,
                "raw_message": body,
                "timestamp": timestamp,
            },
        )
    await MessageService.process_incoming_message(
        db_session,
        "robot-a",
        "qq",
        {
            "message_id": "reply-x",
            "room_id": "group-x",
            "message_type": "group",
            "sender_id": "user-x2",
            "nickname": "X Replier",
            "raw_message": "[CQ:reply,id=2002] same id, other room",
            "timestamp": 1783000010,
        },
    )

    messages = await QueryService.list_messages(
        db_session,
        robot_id="robot-a",
        room_id="group-x",
        limit=50,
    )

    reply = next(item for item in messages if item.external_message_id == "reply-x")
    assert reply.reply_preview_text == "X Target: room x target body"


@pytest.mark.asyncio
async def test_reply_preview_alias_lookup_is_scoped_to_the_account(db_session):
    # The alias fallback resolves through message_source_records and carried the
    # same unscoped defect as the primary lookup. Both accounts here use the
    # *same* room id, which is what a private chat with QQ 7001 looks like from
    # two different accounts, so only the account scope can tell them apart --
    # the room filter cannot.
    for account_id, room_id, nickname, body, hash_suffix in (
        ("robot-a", "friend-7001", "A Imported", "account a imported body", "a"),
        ("robot-b", "friend-7001", "B Imported", "account b confidential body", "b"),
    ):
        source = ImportSource(
            id=f"qqnt-source-{hash_suffix}",
            source_type="qqnt_local_db",
            platform="qq",
            account_id=account_id,
            device_id=f"device-{hash_suffix}",
            status="active",
        )
        message = Message(
            msg_hash=f"aliased-target-{hash_suffix}",
            platform="qq",
            room_id=room_id,
            message_type="private",
            sender_id=f"sender-{hash_suffix}",
            nickname=nickname,
            raw_message=body,
            local_message=body,
            timestamp=1783000000,
        )
        source_record = MessageSourceRecord(
            msg_hash=message.msg_hash,
            source_id=source.id,
            source_table="messages",
            source_primary_key=f"row-{hash_suffix}",
            source_external_message_id="3003",
        )
        db_session.add_all([source, message, source_record])
    await db_session.commit()

    await MessageService.process_incoming_message(
        db_session,
        "robot-a",
        "qq",
        {
            "message_id": "reply-aliased",
            "room_id": "friend-7001",
            "message_type": "private",
            "sender_id": "sender-a",
            "nickname": "A Imported",
            "raw_message": "[CQ:reply,id=3003] follow up",
            "timestamp": 1783000010,
        },
    )

    messages = await QueryService.list_messages(
        db_session,
        robot_id="robot-a",
        room_id="friend-7001",
        limit=50,
    )

    reply = next(item for item in messages if item.external_message_id == "reply-aliased")
    assert reply.reply_preview_text == "A Imported: account a imported body"
    assert "confidential" not in (reply.reply_preview_text or "")


@pytest.mark.asyncio
async def test_reply_preview_resolves_per_room_across_a_cross_room_page(db_session):
    # A search page spans rooms. Reply id "5005" resolves directly in group-p
    # but only through the alias table in friend-p; tracking bare ids instead of
    # (room, id) pairs would treat it as already resolved and silently drop the
    # second room's preview.
    await MessageService.process_incoming_message(
        db_session,
        "robot-a",
        "qq",
        {
            "message_id": "5005",
            "room_id": "group-p",
            "message_type": "group",
            "sender_id": "user-p",
            "nickname": "P Target",
            "raw_message": "direct target body",
            "timestamp": 1783000000,
        },
    )
    source = ImportSource(
        id="qqnt-source-cross-room",
        source_type="qqnt_local_db",
        platform="qq",
        account_id="robot-a",
        device_id="device-cross",
        status="active",
    )
    imported_target = Message(
        msg_hash="cross-room-alias-target",
        platform="qq",
        room_id="friend-p",
        message_type="private",
        sender_id="8001",
        nickname="Q Target",
        raw_message="aliased target body",
        local_message="aliased target body",
        timestamp=1783000001,
    )
    db_session.add_all(
        [
            source,
            imported_target,
            MessageSourceRecord(
                msg_hash=imported_target.msg_hash,
                source_id=source.id,
                source_table="messages",
                source_primary_key="row-cross",
                source_external_message_id="5005",
            ),
        ]
    )
    await db_session.commit()

    for room_id, message_type, sender_id in (("group-p", "group", "user-p2"), ("friend-p", "private", "8001")):
        await MessageService.process_incoming_message(
            db_session,
            "robot-a",
            "qq",
            {
                "message_id": f"reply-{room_id}",
                "room_id": room_id,
                "message_type": message_type,
                "sender_id": sender_id,
                "nickname": "Replier",
                "raw_message": "[CQ:reply,id=5005] needle body",
                "timestamp": 1783000010,
            },
        )

    results = await QueryService.search_messages(db_session, robot_id="robot-a", keyword="needle", limit=50)

    previews = {item.room_id: item.reply_preview_text for item in results}
    assert previews["group-p"] == "P Target: direct target body"
    assert previews["friend-p"] == "Q Target: aliased target body"


@pytest.mark.asyncio
async def test_messages_api_survives_a_sender_whose_avatar_cannot_be_fetched(db_session, monkeypatch):
    # /rooms already isolated per item; /bots and /messages ran the whole page
    # inside one try, so a single unreachable avatar returned 500 for the
    # entire list and the console showed nothing at all.
    await MessageService.process_incoming_message(
        db_session,
        "robot-a",
        "qq",
        {
            "message_id": "hydration-1",
            "room_id": "group-hydrate",
            "message_type": "group",
            "sender_id": "123456789",
            "nickname": "Sender",
            "raw_message": "hello",
            "timestamp": 1783000000,
        },
    )

    async def failing_cache_qq_user_profile(*args, **kwargs):
        raise RuntimeError("avatar asset is not readable")

    monkeypatch.setattr("app.api.UserProfileService.cache_qq_user_profile", failing_cache_qq_user_profile)

    async def override_db_session():
        yield db_session

    app.dependency_overrides[get_db_session] = override_db_session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            messages = await client.get("/api/messages", params={"robot_id": "robot-a", "room_id": "group-hydrate", "limit": 50})
            bots = await client.get("/api/bots")
    finally:
        app.dependency_overrides.clear()

    assert messages.status_code == 200
    assert [item["raw_message"] for item in messages.json()] == ["hello"]
    assert bots.status_code == 200


@pytest.mark.asyncio
async def test_avatar_hydration_is_capped_per_request(db_session, monkeypatch):
    # An uncapped branch meant one serial outbound request per new private chat
    # while the caller waited on the list.
    from app.api import AVATAR_HYDRATION_LIMIT

    for index in range(AVATAR_HYDRATION_LIMIT + 5):
        room_id = str(900000000 + index)
        await MessageService.process_incoming_message(
            db_session,
            "robot-a",
            "qq",
            {
                "room_id": room_id,
                "message_type": "private",
                "sender_id": room_id,
                "nickname": f"Friend {index}",
                "raw_message": "hi",
                "timestamp": 1783000000 + index,
            },
        )

    hydrated: list[str] = []

    async def counting_cache_qq_user_profile(*args, **kwargs):
        hydrated.append(kwargs["user_id"])

    monkeypatch.setattr("app.api.UserProfileService.cache_qq_user_profile", counting_cache_qq_user_profile)

    async def override_db_session():
        yield db_session

    app.dependency_overrides[get_db_session] = override_db_session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/rooms", params={"robot_id": "robot-a"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    # Every room is still returned; only the outbound work is bounded.
    assert len(response.json()) == AVATAR_HYDRATION_LIMIT + 5
    assert len(hydrated) == AVATAR_HYDRATION_LIMIT
