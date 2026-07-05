import json

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import Settings, get_settings
from app.database import get_db_session
from app.main import app
from app.models import MediaAsset, UserProfile
from app.services.message_service import MessageService
from app.services.media_backfill_service import MediaBackfillService
from app.services.offline_audit_service import OfflineAuditService
from app.services.offline_repair_service import OfflineRepairService
from app.services.room_profile_service import RoomProfileService
from app.services.user_profile_service import UserProfileService
from tests.test_media_service import StubAsyncClient


async def seed_profile_avatars(db_session, tmp_path, *, room_id: str, sender_id: str, message_type: str = "group") -> None:
    room_avatar = await MessageService.save_media_asset(
        db_session,
        f"room:{room_id}".encode("utf-8"),
        "image",
        "svg",
        storage_root=tmp_path,
        public_prefix="/static/storage",
    )
    user_avatar = await MessageService.save_media_asset(
        db_session,
        f"user:{sender_id}".encode("utf-8"),
        "image",
        "svg",
        storage_root=tmp_path,
        public_prefix="/static/storage",
    )
    if message_type == "group":
        await RoomProfileService.upsert_room_profile(
            db_session,
            room_id=room_id,
            platform="qq",
            display_name=f"Group {room_id}",
            avatar_path=room_avatar,
        )
    else:
        await UserProfileService.upsert_user_profile(
            db_session,
            user_id=room_id,
            platform="qq",
            display_name=f"User {room_id}",
            avatar_path=room_avatar,
        )
    await UserProfileService.upsert_user_profile(
        db_session,
        user_id=sender_id,
        platform="qq",
        display_name=f"User {sender_id}",
        avatar_path=user_avatar,
    )


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
    await seed_profile_avatars(db_session, tmp_path, room_id="group-ready", sender_id="user-a")

    report = await OfflineAuditService.audit_offline_readiness(
        db_session,
        storage_root=tmp_path,
        public_storage_prefix="/static/storage",
    )

    assert report.offline_ready is True
    assert report.messages_scanned == 1
    assert report.media_assets_checked == 3
    assert report.profile_avatars_checked == 2
    assert report.remote_media_urls == 0
    assert report.uncached_card_pages == 0
    assert report.uncached_forwards == 0
    assert report.missing_profile_avatars == 0
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
    assert report.missing_profile_avatars == 5
    assert report.missing_media_assets == 1
    assert report.missing_media_files == 1
    assert {issue.kind for issue in report.issues} == {"remote_media", "card_page", "forward", "profile_avatar", "media_asset", "media_file"}


@pytest.mark.asyncio
async def test_offline_audit_accepts_finalized_unavailable_media_placeholder(db_session, tmp_path):
    await MessageService.process_incoming_message(
        db_session,
        "robot-a",
        "qq",
        {
            "room_id": "group-finalized",
            "message_type": "group",
            "sender_id": "user-a",
            "nickname": "A",
            "raw_message": "[CQ:image,file=expired.jpg,url=http://media.local/expired.jpg]",
            "timestamp": 1783000000,
        },
    )
    await seed_profile_avatars(db_session, tmp_path, room_id="group-finalized", sender_id="user-a")
    await MediaBackfillService.backfill_historical_media(
        db_session,
        http_client=StubAsyncClient({}),
        storage_root=tmp_path,
        public_prefix="/static/storage",
        finalize_unavailable=True,
    )

    report = await OfflineAuditService.audit_offline_readiness(
        db_session,
        storage_root=tmp_path,
        public_storage_prefix="/static/storage",
    )

    assert report.offline_ready is True
    assert report.remote_media_urls == 0
    assert report.missing_profile_avatars == 0
    assert report.missing_media_assets == 0
    assert report.missing_media_files == 0


@pytest.mark.asyncio
async def test_offline_repair_creates_missing_media_asset_index(db_session, tmp_path):
    media_path = tmp_path / "missing-index.jpg"
    media_path.write_bytes(b"existing local image")
    local_path = "/static/storage/missing-index.jpg"
    await MessageService.process_incoming_message(
        db_session,
        "robot-a",
        "qq",
        {
            "room_id": "group-repair",
            "message_type": "group",
            "sender_id": "user-a",
            "nickname": "A",
            "raw_message": "missing index",
            "local_message": local_path,
            "timestamp": 1783000000,
        },
    )
    await seed_profile_avatars(db_session, tmp_path, room_id="group-repair", sender_id="user-a")

    repair = await OfflineRepairService.repair_local_media_integrity(
        db_session,
        storage_root=tmp_path,
        public_storage_prefix="/static/storage",
    )
    report = await OfflineAuditService.audit_offline_readiness(
        db_session,
        storage_root=tmp_path,
        public_storage_prefix="/static/storage",
    )

    assert repair.scanned_messages == 1
    assert repair.repaired_media_assets == 1
    assert repair.repaired_media_files == 0
    assert repair.repaired_profile_avatars == 0
    assert local_path in repair.repaired_paths
    assert report.offline_ready is True
    assert report.missing_media_assets == 0


@pytest.mark.asyncio
async def test_offline_repair_creates_missing_media_asset_file(db_session, tmp_path):
    local_path = "/static/storage/missing-file.gif"
    db_session.add(MediaAsset(file_hash="missing-file", file_type="image", file_size=999, local_path=local_path))
    await MessageService.process_incoming_message(
        db_session,
        "robot-a",
        "qq",
        {
            "room_id": "group-repair",
            "message_type": "group",
            "sender_id": "user-a",
            "nickname": "A",
            "raw_message": "missing file",
            "local_message": local_path,
            "timestamp": 1783000000,
        },
    )
    await seed_profile_avatars(db_session, tmp_path, room_id="group-repair", sender_id="user-a")
    await db_session.commit()

    repair = await OfflineRepairService.repair_local_media_integrity(
        db_session,
        storage_root=tmp_path,
        public_storage_prefix="/static/storage",
    )
    report = await OfflineAuditService.audit_offline_readiness(
        db_session,
        storage_root=tmp_path,
        public_storage_prefix="/static/storage",
    )

    assert repair.repaired_media_files == 1
    assert repair.repaired_file_sizes == 1
    assert repair.repaired_profile_avatars == 0
    assert local_path in repair.repaired_paths
    assert (tmp_path / "missing-file.gif").exists()
    assert report.offline_ready is True
    assert report.missing_media_files == 0


@pytest.mark.asyncio
async def test_offline_audit_and_repair_cover_profile_avatars(db_session, tmp_path):
    await MessageService.process_incoming_message(
        db_session,
        "robot-a",
        "qq",
        {
            "room_id": "group-avatar",
            "message_type": "group",
            "sender_id": "user-avatar",
            "nickname": "Avatar User",
            "raw_message": "profile avatar",
            "timestamp": 1783000000,
        },
    )

    before = await OfflineAuditService.audit_offline_readiness(
        db_session,
        storage_root=tmp_path,
        public_storage_prefix="/static/storage",
    )
    repair = await OfflineRepairService.repair_local_media_integrity(
        db_session,
        storage_root=tmp_path,
        public_storage_prefix="/static/storage",
    )
    after = await OfflineAuditService.audit_offline_readiness(
        db_session,
        storage_root=tmp_path,
        public_storage_prefix="/static/storage",
    )

    assert before.offline_ready is False
    assert before.profile_avatars_checked == 2
    assert before.missing_profile_avatars == 2
    assert {issue.kind for issue in before.issues} == {"profile_avatar"}
    assert repair.repaired_profile_avatars == 2
    assert after.offline_ready is True
    assert after.profile_avatars_checked == 2
    assert after.missing_profile_avatars == 0
    assert after.missing_media_assets == 0
    assert after.missing_media_files == 0


@pytest.mark.asyncio
async def test_offline_repair_keeps_wechat_profile_platform(db_session, tmp_path):
    await MessageService.process_incoming_message(
        db_session,
        "wxid_123",
        "wechat",
        {
            "room_id": "wx_room",
            "message_type": "private",
            "sender_id": "wx_friend",
            "nickname": "WeChat Friend",
            "raw_message": "wechat profile",
            "timestamp": 1783000000,
        },
    )

    repair = await OfflineRepairService.repair_local_media_integrity(
        db_session,
        storage_root=tmp_path,
        public_storage_prefix="/static/storage",
    )
    room_profile = await db_session.get(UserProfile, "wx_room")
    sender_profile = await db_session.get(UserProfile, "wx_friend")

    assert repair.repaired_profile_avatars == 2
    assert room_profile is not None
    assert room_profile.platform == "wechat"
    assert sender_profile is not None
    assert sender_profile.platform == "wechat"


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
    await seed_profile_avatars(db_session, tmp_path, room_id="group-api", sender_id="user-a")
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
    assert payload["profile_avatars_checked"] == 2
    assert payload["missing_profile_avatars"] == 0
    assert payload["uncached_forwards"] == 1
    assert payload["issues"][0]["kind"] == "forward"


@pytest.mark.asyncio
async def test_offline_repair_api_returns_counts_and_clears_audit(db_session, tmp_path):
    local_path = "/static/storage/api-missing-index.jpg"
    (tmp_path / "api-missing-index.jpg").write_bytes(b"api local image")
    await MessageService.process_incoming_message(
        db_session,
        "robot-a",
        "qq",
        {
            "room_id": "group-api",
            "message_type": "group",
            "sender_id": "user-a",
            "nickname": "A",
            "raw_message": "api repair",
            "local_message": local_path,
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
            repair_response = await client.post("/api/offline/repair", params={"limit": 50000})
            audit_response = await client.get("/api/offline/audit", params={"robot_id": "robot-a"})
    finally:
        app.dependency_overrides.clear()

    assert repair_response.status_code == 200
    repair_payload = repair_response.json()
    assert repair_payload["scanned_messages"] == 1
    assert repair_payload["repaired_media_assets"] == 1
    assert repair_payload["repaired_media_files"] == 0
    assert repair_payload["repaired_file_sizes"] == 0
    assert repair_payload["repaired_profile_avatars"] == 2
    assert local_path in repair_payload["repaired_paths"]
    assert audit_response.status_code == 200
    assert audit_response.json()["offline_ready"] is True
