from httpx import ASGITransport, AsyncClient
import pytest

from app.config import Settings, get_settings
from app.database import get_db_session
from app.main import app
from app.models import AuditLog, BotProfile, MediaAsset, SystemSetting
from app.services.backup_config_service import BackupConfigService
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
        "not_downloaded_media": 0,
        "not_downloaded_videos": 0,
        "thumbnail_only_videos": 0,
        "source_missing_media": 0,
        "media_parse_failures": 0,
        "backups": 1,
        "latest_backup": "auto-backup-20260705T030000Z.json",
    }


@pytest.mark.asyncio
async def test_dashboard_service_counts_completed_v4_but_not_failed_backup(db_session, tmp_path):
    completed = tmp_path / "manual-backup-20260901T000000Z-12345678.cacb"
    completed.write_bytes(b"complete")
    failed = tmp_path / ".manual-backup-20260901T000001Z-12345678.cacb.deadbeef.failed"
    failed.write_bytes(b"failed")

    summary = await DashboardService.get_summary(db_session, backup_root=tmp_path)

    assert summary["backups"] == 1
    assert summary["latest_backup"] == completed.name


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
    payload = response.json()
    # next_run_at moves with the clock, so compare it separately from the
    # settings the endpoint is really reporting.
    next_run_at = payload.pop("next_run_at")
    assert payload == {
        "enabled": True,
        "cron": "15 3 * * *",
        "keep_latest": 3,
        "backup_root": str(tmp_path),
        "backups": 1,
        "latest_backup": "auto-backup-20260705T030000Z.json",
        "config_source": "env",
        "cron_source": "env",
        "keep_latest_source": "env",
        "cron_error": None,
        "worker_healthy": False,
        "active_job": None,
    }
    assert next_run_at.endswith("T03:15:00Z")


@pytest.mark.asyncio
async def test_backup_status_api_reports_a_cron_it_cannot_schedule(db_session, tmp_path):
    # "enabled" only means backups are switched on. Without cron_error the
    # console showed a healthy schedule while the loop could not run a single
    # backup, which is exactly how a mis-set AUTO_BACKUP_CRON stayed invisible.
    settings = Settings(backup_root=tmp_path, auto_backup_cron="0 3 * * 1", auto_backup_keep_latest=3)

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
    payload = response.json()
    assert payload["enabled"] is True
    assert payload["next_run_at"] is None
    assert "day/month/weekday" in payload["cron_error"]


@pytest.mark.asyncio
async def test_backup_status_api_prefers_database_settings(db_session, tmp_path):
    settings = Settings(backup_root=tmp_path, auto_backup_cron="15 3 * * *", auto_backup_keep_latest=3)
    await BackupConfigService.update_config(db_session, settings, cron="45 4 * * *", keep_latest=11)

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
    assert response.json()["cron"] == "45 4 * * *"
    assert response.json()["keep_latest"] == 11
    assert response.json()["config_source"] == "database"
    assert response.json()["cron_source"] == "database"
    assert response.json()["keep_latest_source"] == "database"


@pytest.mark.asyncio
async def test_backup_settings_api_persists_overrides_and_audits(db_session, tmp_path):
    settings = Settings(backup_root=tmp_path, auto_backup_cron="15 3 * * *", auto_backup_keep_latest=3)

    async def override_db_session():
        yield db_session

    app.dependency_overrides[get_db_session] = override_db_session
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.patch("/api/backup/settings", json={"cron": "30 5 * * *", "keep_latest": 9})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["cron"] == "30 5 * * *"
    assert response.json()["keep_latest"] == 9
    assert response.json()["config_source"] == "database"
    assert await db_session.get(SystemSetting, "backup.auto_backup_cron") is not None
    result = await db_session.execute(AuditLog.__table__.select().where(AuditLog.action == "backup.settings.update"))
    audit_log = result.first()
    assert audit_log is not None


@pytest.mark.asyncio
async def test_backup_settings_api_can_reset_to_env(db_session, tmp_path):
    settings = Settings(backup_root=tmp_path, auto_backup_cron="15 3 * * *", auto_backup_keep_latest=3)
    await BackupConfigService.update_config(db_session, settings, cron="30 5 * * *", keep_latest=9)

    async def override_db_session():
        yield db_session

    app.dependency_overrides[get_db_session] = override_db_session
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.patch("/api/backup/settings", json={"reset_to_env": True})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["cron"] == "15 3 * * *"
    assert response.json()["keep_latest"] == 3
    assert response.json()["config_source"] == "env"
    assert await db_session.get(SystemSetting, "backup.auto_backup_cron") is None


@pytest.mark.asyncio
async def test_backup_run_api_only_enqueues_the_isolated_worker(db_session, tmp_path):
    from app.backup.jobs import write_worker_heartbeat

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
    write_worker_heartbeat(backup_root)

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
    payload = response.json()
    assert payload["state"] == "queued"
    assert payload["backup_type"] == "manual"
    assert payload["filename"] is None
    assert not list(backup_root.glob("*.cacb"))
    assert (backup_root / ".backup-jobs" / f"{payload['job_id']}.request.json").exists()


@pytest.mark.asyncio
async def test_backup_run_api_rejects_when_isolated_worker_is_unavailable(db_session, tmp_path):
    settings = Settings(backup_root=tmp_path / "backups")

    async def override_db_session():
        yield db_session

    app.dependency_overrides[get_db_session] = override_db_session
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/backup/run")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503
    assert not list((tmp_path / "backups" / ".backup-jobs").glob("*.request.json"))
