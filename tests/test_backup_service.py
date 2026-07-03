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


@pytest.mark.asyncio
async def test_backup_service_writes_auto_backup_file_and_prunes_old_files(db_session, tmp_path):
    await MessageService.process_incoming_message(
        db_session,
        "robot-auto",
        "qq",
        {
            "room_id": "room-auto",
            "message_type": "group",
            "sender_id": "user-auto",
            "nickname": "Auto User",
            "raw_message": "auto backup payload",
            "timestamp": 456,
        },
    )
    old_a = tmp_path / "auto-backup-20000101T000000Z.json"
    old_b = tmp_path / "auto-backup-20000102T000000Z.json"
    old_a.write_text("{}", encoding="utf-8")
    old_b.write_text("{}", encoding="utf-8")

    backup_path = await BackupService.write_auto_backup_file(
        db_session,
        backup_root=tmp_path,
        keep_latest=2,
    )

    assert backup_path.exists()
    assert backup_path.name.startswith("auto-backup-")
    assert backup_path.suffix == ".json"
    package = __import__("json").loads(backup_path.read_text(encoding="utf-8"))
    assert package["manifest"]["schema"] == "chat-audit-core.backup.v1"
    assert package["manifest"]["backup_type"] == "auto"
    assert package["messages"][0]["raw_message"] == "auto backup payload"
    remaining = sorted(path.name for path in tmp_path.glob("auto-backup-*.json"))
    assert remaining == sorted([old_b.name, backup_path.name])


def test_backup_service_calculates_next_daily_cron_run():
    now = __import__("datetime").datetime(2026, 7, 3, 2, 30, 0)

    next_run = BackupService.next_run_from_cron("15 3 * * *", now)

    assert next_run == __import__("datetime").datetime(2026, 7, 3, 3, 15, 0)


def test_backup_service_calculates_tomorrow_when_daily_cron_time_has_passed():
    now = __import__("datetime").datetime(2026, 7, 3, 4, 0, 0)

    next_run = BackupService.next_run_from_cron("15 3 * * *", now)

    assert next_run == __import__("datetime").datetime(2026, 7, 4, 3, 15, 0)


@pytest.mark.asyncio
async def test_backup_service_export_includes_package_checksum(db_session):
    await MessageService.process_incoming_message(
        db_session,
        "robot-checksum",
        "qq",
        {
            "room_id": "room-checksum",
            "message_type": "group",
            "sender_id": "user-checksum",
            "nickname": "Checksum User",
            "raw_message": "checksum payload",
            "timestamp": 789,
        },
    )

    package = await BackupService.export_package(db_session, robot_id="robot-checksum")

    checksum = package["manifest"]["checksum"]
    assert checksum["algorithm"] == "sha256"
    assert len(checksum["value"]) == 64
    assert checksum["value"] == BackupService.calculate_package_checksum(package)


@pytest.mark.asyncio
async def test_backup_service_import_rejects_checksum_mismatch(db_session):
    package = {
        "manifest": {
            "schema": "chat-audit-core.backup.v1",
            "checksum": {"algorithm": "sha256", "value": "0" * 64},
        },
        "messages": [
            {
                "msg_hash": "hash-checksum",
                "platform": "qq",
                "room_id": "room-checksum",
                "message_type": "group",
                "sender_id": "user-checksum",
                "nickname": "Checksum User",
                "raw_message": "tampered",
                "local_message": "tampered",
                "timestamp": 999,
            }
        ],
        "robot_messages": [{"robot_id": "robot-checksum", "msg_hash": "hash-checksum"}],
        "media_assets": [],
    }

    with pytest.raises(ValueError, match="checksum mismatch"):
        await BackupService.import_package(db_session, package)


@pytest.mark.asyncio
async def test_import_validate_api_reports_valid_package_without_writing(db_session):
    await MessageService.process_incoming_message(
        db_session,
        "robot-validate",
        "qq",
        {
            "room_id": "room-validate",
            "message_type": "group",
            "sender_id": "user-validate",
            "nickname": "Validate User",
            "raw_message": "validate payload",
            "timestamp": 1001,
        },
    )
    package = await BackupService.export_package(db_session, robot_id="robot-validate")

    async def override_db_session():
        yield db_session

    app.dependency_overrides[get_db_session] = override_db_session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/import/validate", json=package)
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    report = response.json()
    assert report["valid"] is True
    assert report["schema"] == "chat-audit-core.backup.v1"
    assert report["checksum_valid"] is True
    assert report["errors"] == []
    assert report["counts"] == {"messages": 1, "robot_messages": 1, "media_assets": 0}


@pytest.mark.asyncio
async def test_import_validate_api_reports_checksum_mismatch(db_session):
    package = {
        "manifest": {
            "schema": "chat-audit-core.backup.v1",
            "checksum": {"algorithm": "sha256", "value": "0" * 64},
        },
        "messages": [],
        "robot_messages": [],
        "media_assets": [],
    }

    async def override_db_session():
        yield db_session

    app.dependency_overrides[get_db_session] = override_db_session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/import/validate", json=package)
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    report = response.json()
    assert report["valid"] is False
    assert report["checksum_valid"] is False
    assert any("checksum mismatch" in error for error in report["errors"])
