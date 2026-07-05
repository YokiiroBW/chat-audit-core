from httpx import ASGITransport, AsyncClient
import pytest

from app.config import Settings, get_settings
from app.database import get_db_session
from app.main import app
from app.models import BotProfile, MediaAsset
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
