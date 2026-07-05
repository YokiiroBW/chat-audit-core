import json

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import Settings, get_settings
from app.database import get_db_session
from app.main import app
from app.models import MediaAsset
from app.services.message_service import MessageService
from app.services.offline_audit_service import OfflineAuditService


@pytest.mark.asyncio
async def test_offline_audit_reports_ready_when_messages_use_existing_local_assets(db_session, tmp_path):
    local_path = await MessageService.save_media_asset(
        db_session,
        b"local image",
        "image",
        "jpg",
        storage_root=tmp_path,
        public_prefix="/static/storage",
    )
    await MessageService.process_incoming_message(
        db_session,
        "robot-a",
        "qq",
        {
            "room_id": "group-ready",
            "message_type": "group",
            "sender_id": "user-a",
            "nickname": "A",
            "raw_message": "ready",
            "local_message": local_path,
            "timestamp": 1783000000,
        },
    )

    report = await OfflineAuditService.audit_offline_readiness(
        db_session,
        storage_root=tmp_path,
        public_storage_prefix="/static/storage",
    )

    assert report.offline_ready is True
    assert report.messages_scanned == 1
    assert report.media_assets_checked == 1
    assert report.remote_media_urls == 0
    assert report.uncached_card_pages == 0
    assert report.uncached_forwards == 0
    assert report.missing_media_assets == 0
    assert report.missing_media_files == 0
    assert report.issues == []


@pytest.mark.asyncio
async def test_offline_audit_reports_remote_and_missing_local_assets(db_session, tmp_path):
    card = {"meta": {"detail_1": {"title": "Card", "url": "https://example.com/page"}}}
    raw_card = f"[CQ:json,data={json.dumps(card, ensure_ascii=False).replace(',', '&#44;')}]"
    await MessageService.process_incoming_message(
        db_session,
        "robot-a",
        "qq",
        {
            "room_id": "group-issue",
            "message_type": "group",
            "sender_id": "user-a",
            "nickname": "A",
            "raw_message": "[CQ:image,file=old.jpg,url=http://media.local/old.jpg]",
            "timestamp": 1783000000,
        },
    )
    await MessageService.process_incoming_message(
        db_session,
        "robot-a",
        "qq",
        {
            "room_id": "group-issue",
            "message_type": "group",
            "sender_id": "user-b",
            "nickname": "B",
            "raw_message": raw_card,
            "timestamp": 1783000001,
        },
    )
    await MessageService.process_incoming_message(
        db_session,
        "robot-a",
        "qq",
        {
            "room_id": "group-issue",
            "message_type": "group",
            "sender_id": "user-c",
            "nickname": "C",
            "raw_message": "[CQ:forward,id=forward-1]",
            "timestamp": 1783000002,
        },
    )
    await MessageService.process_incoming_message(
        db_session,
        "robot-a",
        "qq",
        {
            "room_id": "group-issue",
            "message_type": "group",
            "sender_id": "user-d",
            "nickname": "D",
            "raw_message": "missing asset",
            "local_message": "/static/storage/missing-index.jpg",
            "timestamp": 1783000003,
        },
    )
    db_session.add(MediaAsset(file_hash="missing-file", file_type="image", file_size=10, local_path="/static/storage/missing-file.jpg"))
    await db_session.commit()

    report = await OfflineAuditService.audit_offline_readiness(
        db_session,
        storage_root=tmp_path,
        public_storage_prefix="/static/storage",
    )

    assert report.offline_ready is False
    assert report.remote_media_urls == 1
    assert report.uncached_card_pages == 1
    assert report.uncached_forwards == 1
    assert report.missing_media_assets == 1
    assert report.missing_media_files == 1
    assert {issue.kind for issue in report.issues} == {"remote_media", "card_page", "forward", "media_asset", "media_file"}


@pytest.mark.asyncio
async def test_offline_audit_api_returns_report(db_session, tmp_path):
    await MessageService.process_incoming_message(
        db_session,
        "robot-a",
        "qq",
        {
            "room_id": "group-api",
            "message_type": "group",
            "sender_id": "user-a",
            "nickname": "A",
            "raw_message": "[CQ:forward,id=forward-1]",
            "timestamp": 1783000000,
        },
    )
    settings = Settings(storage_root=tmp_path, public_storage_prefix="/static/storage")

    async def override_db_session():
        yield db_session

    app.dependency_overrides[get_db_session] = override_db_session
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/offline/audit", params={"robot_id": "robot-a"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["offline_ready"] is False
    assert payload["messages_scanned"] == 1
    assert payload["uncached_forwards"] == 1
    assert payload["issues"][0]["kind"] == "forward"
