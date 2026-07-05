import pytest
from httpx import ASGITransport, AsyncClient

from app.config import Settings, get_settings
from app.database import get_db_session
from app.main import app
from app.services.media_backfill_service import MediaBackfillService
from app.services.message_service import MessageService
from tests.test_media_service import StubAsyncClient


@pytest.mark.asyncio
async def test_backfill_historical_media_rewrites_old_cq_asset(db_session, tmp_path):
    client = StubAsyncClient({"http://media.local/old.jpg": b"old image"})
    await MessageService.process_incoming_message(
        db_session,
        "robot-a",
        "qq",
        {
            "room_id": "group-old",
            "message_type": "group",
            "sender_id": "user-a",
            "nickname": "A",
            "raw_message": "[CQ:image,file=old.jpg,url=http://media.local/old.jpg]",
            "timestamp": 1783000000,
        },
    )

    report = await MediaBackfillService.backfill_historical_media(
        db_session,
        http_client=client,
        storage_root=tmp_path,
        public_prefix="/static/storage",
    )

    messages = await MessageService.list_messages(db_session)
    assets = await MessageService.list_media_assets(db_session)

    assert report.scanned == 1
    assert report.candidates == 1
    assert report.updated == 1
    assert report.failed == 0
    assert messages[0].local_message.startswith("/static/storage/")
    assert "http://media.local" not in messages[0].local_message
    assert len(assets) == 1


@pytest.mark.asyncio
async def test_backfill_historical_media_reports_expired_url_without_overwriting(db_session, tmp_path):
    client = StubAsyncClient({})
    raw = "[CQ:image,file=expired.jpg,url=http://media.local/expired.jpg]"
    await MessageService.process_incoming_message(
        db_session,
        "robot-a",
        "qq",
        {
            "room_id": "group-old",
            "message_type": "group",
            "sender_id": "user-a",
            "nickname": "A",
            "raw_message": raw,
            "timestamp": 1783000000,
        },
    )

    report = await MediaBackfillService.backfill_historical_media(
        db_session,
        http_client=client,
        storage_root=tmp_path,
        public_prefix="/static/storage",
    )

    messages = await MessageService.list_messages(db_session)

    assert report.updated == 0
    assert report.unchanged == 1
    assert report.failed == 1
    assert report.media_failed == 1
    assert report.failures[0].target == "http://media.local/expired.jpg"
    assert messages[0].local_message == raw


@pytest.mark.asyncio
async def test_backfill_historical_media_caches_unclicked_forward_payload(db_session, tmp_path):
    client = StubAsyncClient({"http://media.local/in-forward.jpg": b"inside forward"})
    seen: list[tuple[str, str]] = []

    async def load_forward(robot_id: str, forward_id: str):
        seen.append((robot_id, forward_id))
        return {
            "status": "ok",
            "data": {
                "messages": [
                    {
                        "raw_message": "[CQ:image,file=f.jpg,url=http://media.local/in-forward.jpg]",
                    }
                ]
            },
        }

    await MessageService.process_incoming_message(
        db_session,
        "robot-forward",
        "qq",
        {
            "room_id": "group-forward",
            "message_type": "group",
            "sender_id": "user-forward",
            "nickname": "Forward User",
            "raw_message": "[CQ:forward,id=forward-1]",
            "timestamp": 1783000000,
        },
    )

    report = await MediaBackfillService.backfill_historical_media(
        db_session,
        http_client=client,
        storage_root=tmp_path,
        public_prefix="/static/storage",
        forward_payload_loader=load_forward,
    )

    messages = await MessageService.list_messages(db_session)
    assets = await MessageService.list_media_assets(db_session)

    assert seen == [("robot-forward", "forward-1")]
    assert report.updated == 1
    assert report.forward_failed == 0
    assert "local=/static/storage/" in messages[0].local_message
    assert {asset.file_type for asset in assets} == {"image", "forward"}


@pytest.mark.asyncio
async def test_media_backfill_api_supports_dry_run(db_session, tmp_path):
    await MessageService.process_incoming_message(
        db_session,
        "robot-a",
        "qq",
        {
            "room_id": "group-old",
            "message_type": "group",
            "sender_id": "user-a",
            "nickname": "A",
            "raw_message": "[CQ:image,file=old.jpg,url=http://media.local/old.jpg]",
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
            response = await client.post("/api/media/backfill", params={"dry_run": True})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["scanned"] == 1
    assert payload["candidates"] == 1
    assert payload["updated"] == 0
    assert payload["unchanged"] == 1
    assert payload["failures"] == []
