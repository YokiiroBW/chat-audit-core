from httpx import ASGITransport, AsyncClient
import pytest

from app.database import get_db_session
from app.main import app
from app.models import MediaAsset
from app.services.backup_service import BackupService
from app.services.message_service import MessageService


@pytest.mark.asyncio
async def test_backup_service_exports_filtered_messages_and_manifest(db_session):
    await MessageService.process_incoming_message(
        db_session,
        "robot-a",
        "qq",
        {
            "room_id": "room-1",
            "message_type": "group",
            "sender_id": "user-1",
            "nickname": "Alice",
            "raw_message": "export me",
            "local_message": "/static/storage/media-a.jpg",
            "timestamp": 100,
        },
    )
    await MessageService.process_incoming_message(
        db_session,
        "robot-b",
        "qq",
        {
            "room_id": "room-2",
            "message_type": "group",
            "sender_id": "user-2",
            "nickname": "Bob",
            "raw_message": "do not export",
            "timestamp": 200,
        },
    )
    db_session.add(
        MediaAsset(
            file_hash="media-a",
            file_type="image",
            file_size=10,
            local_path="/static/storage/media-a.jpg",
        )
    )
    await db_session.commit()

    package = await BackupService.export_package(
        db_session,
        robot_id="robot-a",
        room_id="room-1",
        start_timestamp=50,
        end_timestamp=150,
    )

    assert package["manifest"]["schema"] == "chat-audit-core.backup.v1"
    assert package["manifest"]["filters"] == {
        "robot_id": "robot-a",
        "room_id": "room-1",
        "start_timestamp": 50,
        "end_timestamp": 150,
    }
    assert len(package["messages"]) == 1
    assert package["messages"][0]["raw_message"] == "export me"
    assert package["robot_messages"] == [{"robot_id": "robot-a", "msg_hash": package["messages"][0]["msg_hash"]}]
    assert package["media_assets"] == [
        {
            "file_hash": "media-a",
            "file_type": "image",
            "file_size": 10,
            "local_path": "/static/storage/media-a.jpg",
        }
    ]


@pytest.mark.asyncio
async def test_backup_service_import_upserts_without_duplicates(db_session):
    package = {
        "manifest": {"schema": "chat-audit-core.backup.v1"},
        "messages": [
            {
                "msg_hash": "hash-1",
                "platform": "qq",
                "room_id": "room-import",
                "message_type": "group",
                "sender_id": "user-import",
                "nickname": "Old Name",
                "raw_message": "hello",
                "local_message": "hello",
                "timestamp": 123,
            }
        ],
        "robot_messages": [{"robot_id": "robot-import", "msg_hash": "hash-1"}],
        "media_assets": [
            {
                "file_hash": "media-import",
                "file_type": "image",
                "file_size": 5,
                "local_path": "/static/storage/media-import.jpg",
            }
        ],
    }

    first_result = await BackupService.import_package(db_session, package)
    package["messages"][0]["nickname"] = "New Name"
    second_result = await BackupService.import_package(db_session, package)

    messages = await MessageService.list_messages(db_session)
    robot_messages = await MessageService.list_robot_messages(db_session)
    media_assets = await MessageService.list_media_assets(db_session)

    assert first_result == {"messages": 1, "robot_messages": 1, "media_assets": 1}
    assert second_result == {"messages": 1, "robot_messages": 1, "media_assets": 1}
    assert len(messages) == 1
    assert messages[0].nickname == "New Name"
    assert len(robot_messages) == 1
    assert len(media_assets) == 1


@pytest.mark.asyncio
async def test_export_import_api_roundtrip(db_session):
    await MessageService.process_incoming_message(
        db_session,
        "robot-api",
        "qq",
        {
            "room_id": "room-api",
            "message_type": "group",
            "sender_id": "user-api",
            "nickname": "API User",
            "raw_message": "api export",
            "timestamp": 321,
        },
    )

    async def override_db_session():
        yield db_session

    app.dependency_overrides[get_db_session] = override_db_session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            export_response = await client.get("/api/export", params={"robot_id": "robot-api", "room_id": "room-api"})
            import_response = await client.post("/api/import", json=export_response.json())
    finally:
        app.dependency_overrides.clear()

    assert export_response.status_code == 200
    assert export_response.json()["messages"][0]["raw_message"] == "api export"
    assert import_response.status_code == 200
    assert import_response.json() == {"messages": 1, "robot_messages": 1, "media_assets": 0}
