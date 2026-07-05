from httpx import ASGITransport, AsyncClient
import pytest

from app.config import Settings, get_settings
from app.database import get_db_session
from app.main import app
from app.models import BotProfile, MediaAsset
from app.services.backup_service import BackupService
from app.services.dashboard_service import DashboardService
from app.services.message_service import MessageService


@pytest.mark.asyncio
async def test_dashboard_service_returns_global_summary(db_session, tmp_path):
    backup_file = tmp_path / "auto-backup-20260705T030000Z.json"
    backup_file.write_text("{}", encoding="utf-8")
    db_session.add(BotProfile(id="robot-a", platform="qq"))
    db_session.add(MediaAsset(file_hash="media-a", file_type="image", file_size=12, local_path="/static/storage/media-a.jpg"))
    await MessageService.process_incoming_message(
        db_session,
        "robot-a",
        "qq",
        {
            "room_id": "room-a",
            "message_type": "group",
            "sender_id": "user-a",
            "nickname": "A",
            "raw_message": "dashboard one",
            "timestamp": 1783000000,
        },
    )
    await MessageService.process_incoming_message(
        db_session,
        "robot-a",
        "qq",
        {
            "room_id": "room-b",
            "message_type": "private",
            "sender_id": "user-b",
            "nickname": "B",
            "raw_message": "dashboard two",
            "timestamp": 1783000001,
        },
    )

    summary = await DashboardService.get_summary(db_session, backup_root=tmp_path)

    assert summary == {
        "bots": 1,
        "rooms": 2,
        "messages": 2,
        "robot_views": 2,
        "media_assets": 1,
        "media_bytes": 12,
        "backups": 1,
        "latest_backup": "auto-backup-20260705T030000Z.json",
    }


@pytest.mark.asyncio
async def test_dashboard_api_returns_summary(db_session, tmp_path):
    settings = Settings(backup_root=tmp_path)
    db_session.add(BotProfile(id="robot-api", platform="qq"))
    await db_session.commit()

    async def override_db_session():
        yield db_session

    app.dependency_overrides[get_db_session] = override_db_session
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/dashboard")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["bots"] == 1
    assert response.json()["messages"] == 0
    assert response.json()["backups"] == 0


@pytest.mark.asyncio
async def test_backup_status_api_returns_scheduler_settings(db_session, tmp_path):
    backup_file = tmp_path / "auto-backup-20260705T030000Z.json"
    backup_file.write_text("{}", encoding="utf-8")
    settings = Settings(backup_root=tmp_path, auto_backup_cron="15 3 * * *", auto_backup_keep_latest=3)

    async def override_db_session():
        yield db_session

    app.dependency_overrides[get_db_session] = override_db_session
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/backup/status")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "enabled": True,
        "cron": "15 3 * * *",
        "keep_latest": 3,
        "backup_root": str(tmp_path),
        "backups": 1,
        "latest_backup": "auto-backup-20260705T030000Z.json",
    }


@pytest.mark.asyncio
async def test_backup_run_api_writes_signed_backup_file(db_session, tmp_path):
    storage_root = tmp_path / "storage"
    backup_root = tmp_path / "backups"
    settings = Settings(
        storage_root=storage_root,
        backup_root=backup_root,
        app_secret_key="test-secret",
        system_instance_id="test-instance",
    )
    await MessageService.process_incoming_message(
        db_session,
        "robot-backup-api",
        "qq",
        {
            "room_id": "room-backup-api",
            "message_type": "group",
            "sender_id": "user-backup-api",
            "nickname": "Backup API User",
            "raw_message": "backup api payload",
            "timestamp": 1783000100,
        },
    )

    async def override_db_session():
        yield db_session

    app.dependency_overrides[get_db_session] = override_db_session
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/backup/run")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    backup_path = backup_root / response.json()["filename"]
    assert backup_path.exists()
    package = __import__("json").loads(backup_path.read_text(encoding="utf-8"))
    assert package["manifest"]["source"]["instance_id"] == "test-instance"
    BackupService.validate_package_signature(package, "test-secret")
