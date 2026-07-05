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
    assert report["counts"] == {
        "messages": 1,
        "robot_messages": 1,
        "media_assets": 0,
        "media_files": 0,
        "room_profiles": 0,
        "user_profiles": 0,
    }


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


@pytest.mark.asyncio
async def test_import_api_rejects_malformed_package_with_400(db_session, tmp_path, monkeypatch):
    package = {
        "manifest": {"schema": "chat-audit-core.backup.v1"},
        "messages": [{"platform": "qq"}],
        "robot_messages": [],
        "media_assets": [],
    }

    from app.config import Settings
    from app.api import get_settings

    settings = Settings(storage_root=tmp_path / "storage", backup_root=tmp_path / "backups")

    async def override_db_session():
        yield db_session

    app.dependency_overrides[get_db_session] = override_db_session
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/import", json=package)
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 400
    assert "missing required field" in response.json()["detail"]
    assert (tmp_path / "backups" / "failures.log").exists()


@pytest.mark.asyncio
async def test_import_validate_api_reports_database_diff_preview(db_session):
    await MessageService.process_incoming_message(
        db_session,
        "robot-existing",
        "qq",
        {
            "room_id": "room-existing",
            "message_type": "group",
            "sender_id": "user-existing",
            "nickname": "Existing User",
            "raw_message": "old payload",
            "local_message": "old payload",
            "timestamp": 111,
        },
    )
    existing_package = await BackupService.export_package(db_session, robot_id="robot-existing")
    existing_hash = existing_package["messages"][0]["msg_hash"]
    package = {
        "manifest": {"schema": "chat-audit-core.backup.v1"},
        "messages": [
            {
                "msg_hash": existing_hash,
                "platform": "qq",
                "room_id": "room-existing",
                "message_type": "group",
                "sender_id": "user-existing",
                "nickname": "Existing User Updated",
                "raw_message": "updated payload",
                "local_message": "updated payload",
                "timestamp": 222,
            },
            {
                "msg_hash": "hash-new-message",
                "platform": "qq",
                "room_id": "room-new",
                "message_type": "group",
                "sender_id": "user-new",
                "nickname": "New User",
                "raw_message": "new payload",
                "local_message": "new payload",
                "timestamp": 333,
            },
        ],
        "robot_messages": [
            {"robot_id": "robot-existing", "msg_hash": existing_hash},
            {"robot_id": "robot-existing", "msg_hash": "hash-new-message"},
        ],
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
    assert report["valid"] is True
    assert report["diff"]["messages"] == {"new": 1, "update": 1, "unchanged": 0}
    assert report["diff"]["robot_messages"] == {"new": 1, "existing": 1}


def test_backup_service_validates_media_file_checksum(tmp_path):
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    media_file = storage_root / "media-ok.bin"
    media_file.write_bytes(b"media payload")
    checksum = __import__("hashlib").sha256(b"media payload").hexdigest()
    package = {
        "manifest": {"schema": "chat-audit-core.backup.v1"},
        "messages": [],
        "robot_messages": [],
        "media_assets": [
            {
                "file_hash": "media-ok",
                "file_type": "image",
                "file_size": len(b"media payload"),
                "local_path": "/static/storage/media-ok.bin",
                "file_checksum": {"algorithm": "sha256", "value": checksum},
            }
        ],
    }

    report = BackupService.validate_import_package(
        package,
        storage_root=storage_root,
        public_storage_prefix="/static/storage",
    )

    assert report["valid"] is True
    assert report["media_files"] == {"checked": 1, "missing": 0, "mismatch": 0}


@pytest.mark.asyncio
async def test_backup_service_exports_and_restores_embedded_media_file(db_session, tmp_path):
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    media_file = storage_root / "media-restore.jpg"
    media_content = b"restorable media payload"
    media_file.write_bytes(media_content)

    await MessageService.process_incoming_message(
        db_session,
        "robot-media-backup",
        "qq",
        {
            "room_id": "room-media-backup",
            "message_type": "group",
            "sender_id": "user-media-backup",
            "nickname": "Media Backup User",
            "raw_message": "photo",
            "local_message": "before /static/storage/media-restore.jpg after",
            "timestamp": 1500,
        },
    )
    db_session.add(
        MediaAsset(
            file_hash="media-restore",
            file_type="image",
            file_size=len(media_content),
            local_path="/static/storage/media-restore.jpg",
        )
    )
    await db_session.commit()

    package = await BackupService.export_package(
        db_session,
        robot_id="robot-media-backup",
        storage_root=storage_root,
        public_storage_prefix="/static/storage",
    )
    media_file.unlink()

    await BackupService.import_package(
        db_session,
        package,
        storage_root=storage_root,
        public_storage_prefix="/static/storage",
    )

    assert package["manifest"]["counts"]["media_files"] == 1
    assert package["media_assets"][0]["file_checksum"]["algorithm"] == "sha256"
    assert package["media_files"][0]["local_path"] == "/static/storage/media-restore.jpg"
    assert media_file.read_bytes() == media_content


@pytest.mark.asyncio
async def test_backup_service_skips_embedding_media_file_over_size_limit(db_session, tmp_path):
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    media_file = storage_root / "media-large.jpg"
    media_file.write_bytes(b"large media payload")

    await MessageService.process_incoming_message(
        db_session,
        "robot-media-large",
        "qq",
        {
            "room_id": "room-media-large",
            "message_type": "group",
            "sender_id": "user-media-large",
            "nickname": "Media Large User",
            "raw_message": "photo",
            "local_message": "/static/storage/media-large.jpg",
            "timestamp": 1600,
        },
    )
    db_session.add(
        MediaAsset(
            file_hash="media-large",
            file_type="image",
            file_size=19,
            local_path="/static/storage/media-large.jpg",
        )
    )
    await db_session.commit()

    package = await BackupService.export_package(
        db_session,
        robot_id="robot-media-large",
        storage_root=storage_root,
        public_storage_prefix="/static/storage",
        max_media_bytes=4,
    )

    assert package["manifest"]["counts"]["media_assets"] == 1
    assert package["manifest"]["counts"]["media_files"] == 0
    assert package["media_files"] == []


def test_backup_service_reports_media_file_checksum_mismatch(tmp_path):
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    media_file = storage_root / "media-bad.bin"
    media_file.write_bytes(b"actual payload")
    package = {
        "manifest": {"schema": "chat-audit-core.backup.v1"},
        "messages": [],
        "robot_messages": [],
        "media_assets": [
            {
                "file_hash": "media-bad",
                "file_type": "image",
                "file_size": 999,
                "local_path": "/static/storage/media-bad.bin",
                "file_checksum": {"algorithm": "sha256", "value": "0" * 64},
            }
        ],
    }

    report = BackupService.validate_import_package(
        package,
        storage_root=storage_root,
        public_storage_prefix="/static/storage",
    )

    assert report["valid"] is False
    assert report["media_files"] == {"checked": 1, "missing": 0, "mismatch": 1}
    assert any("media checksum mismatch" in error for error in report["errors"])


def test_backup_service_writes_failure_log(tmp_path):
    log_path = BackupService.write_failure_log(
        tmp_path,
        event="import",
        error="checksum mismatch",
        context={"schema": "chat-audit-core.backup.v1"},
    )

    assert log_path == tmp_path / "failures.log"
    line = log_path.read_text(encoding="utf-8").strip()
    record = __import__("json").loads(line)
    assert record["event"] == "import"
    assert record["error"] == "checksum mismatch"
    assert record["context"] == {"schema": "chat-audit-core.backup.v1"}
    assert record["created_at"].endswith("Z")
