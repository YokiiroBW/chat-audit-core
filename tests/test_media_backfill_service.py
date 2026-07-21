import asyncio
import html
import json

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import Settings, get_settings
from app.database import get_db_session
from app.main import app
from app.models import MessageMediaReference
from app.services.media_backfill_service import MediaBackfillService
from app.services.message_service import MessageService
from tests.test_media_service import StubAsyncClient


@pytest.mark.asyncio
async def test_backfill_historical_media_waits_for_existing_backfill(db_session):
    await MediaBackfillService._backfill_lock.acquire()
    task = asyncio.create_task(MediaBackfillService.backfill_historical_media(db_session, dry_run=True))
    try:
        await asyncio.sleep(0)
        assert task.done() is False
    finally:
        MediaBackfillService._backfill_lock.release()

    report = await task

    assert report.scanned == 0


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
    assert report.reason_summary == {"download_failed_or_expired": 1}
    assert report.failures[0].target == "http://media.local/expired.jpg"
    assert report.failures[0].label == "媒体下载失败或源地址已过期"
    assert messages[0].local_message == raw


@pytest.mark.asyncio
async def test_backfill_historical_media_can_finalize_expired_url_with_placeholder(db_session, tmp_path):
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
        finalize_unavailable=True,
    )

    messages = await MessageService.list_messages(db_session)
    assets = await MessageService.list_media_assets(db_session)

    assert report.updated == 1
    assert report.failed == 0
    assert messages[0].local_message.startswith("/static/storage/")
    assert messages[0].local_message.endswith(".svg")
    assert assets[0].file_type == "image_missing"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("prefix", "structured_ordinal"),
    [
        ("", 0),
        ("/static/storage/already-local.jpg\n", 1),
    ],
)
async def test_backfill_historical_media_skips_only_structured_media_ordinal(
    db_session,
    tmp_path,
    prefix,
    structured_ordinal,
):
    structured_url = "http://media.local/not-downloaded.mp4"
    unstructured_url = "http://media.local/extra.jpg"
    raw = (
        f"{prefix}[CQ:video,file=missing.mp4,url={structured_url}]"
        f"[CQ:image,file=extra.jpg,url={unstructured_url}]"
    )
    msg_hash = await MessageService.process_incoming_message(
        db_session,
        "robot-a",
        "qq",
        {
            "room_id": "group-mixed",
            "message_type": "group",
            "sender_id": "user-a",
            "nickname": "A",
            "raw_message": raw,
            "timestamp": 1783000000,
        },
    )
    db_session.add(
        MessageMediaReference(
            msg_hash=msg_hash,
            ordinal=structured_ordinal,
            media_type="video",
            source_state="not_downloaded",
            archive_state="metadata_only",
        )
    )
    await db_session.commit()
    client = StubAsyncClient({unstructured_url: b"extra image"})

    report = await MediaBackfillService.backfill_historical_media(
        db_session,
        http_client=client,
        storage_root=tmp_path,
        public_prefix="/static/storage",
        finalize_unavailable=True,
    )

    messages = await MessageService.list_messages(db_session)
    assets = await MessageService.list_media_assets(db_session)

    assert client.requested_urls == [unstructured_url]
    assert report.updated == 1
    assert report.failed == 0
    assert structured_url in messages[0].local_message
    assert unstructured_url not in messages[0].local_message
    assert len(assets) == 1
    assert assets[0].file_type == "image"
    assert assets[0].file_size > 0


@pytest.mark.asyncio
async def test_backfill_historical_media_preserves_skipped_duplicate_segment(db_session, tmp_path):
    shared_url = "http://media.local/shared.mp4"
    segment = f"[CQ:video,file=shared.mp4,url={shared_url}]"
    raw = segment + segment
    msg_hash = await MessageService.process_incoming_message(
        db_session,
        "robot-a",
        "qq",
        {
            "room_id": "group-duplicate",
            "message_type": "group",
            "sender_id": "user-a",
            "nickname": "A",
            "raw_message": raw,
            "timestamp": 1783000001,
        },
    )
    db_session.add(
        MessageMediaReference(
            msg_hash=msg_hash,
            ordinal=0,
            media_type="video",
            source_state="not_downloaded",
            archive_state="metadata_only",
        )
    )
    await db_session.commit()
    client = StubAsyncClient({shared_url: b"video"})

    report = await MediaBackfillService.backfill_historical_media(
        db_session,
        http_client=client,
        storage_root=tmp_path,
        public_prefix="/static/storage",
        finalize_unavailable=True,
    )

    messages = await MessageService.list_messages(db_session)
    assert report.updated == 1
    assert client.requested_urls == [shared_url]
    assert messages[0].local_message.count(shared_url) == 1
    assert messages[0].local_message.count("/static/storage/") == 1


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
async def test_backfill_historical_media_can_finalize_uncached_forward(db_session, tmp_path):
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
        storage_root=tmp_path,
        public_prefix="/static/storage",
        finalize_unavailable=True,
    )

    messages = await MessageService.list_messages(db_session)
    assets = await MessageService.list_media_assets(db_session)

    assert report.updated == 1
    assert report.failed == 0
    assert "local=/static/storage/" in messages[0].local_message
    assert assets[0].file_type == "forward_missing"


@pytest.mark.asyncio
async def test_backfill_historical_media_caches_old_card_page_snapshot(db_session, tmp_path):
    client = StubAsyncClient({"https://example.com/page": b"<html>card snapshot</html>"})
    card = {"meta": {"detail_1": {"title": "Card", "preview": "/static/storage/existing.jpg", "url": "https://example.com/page"}}}
    raw = f"[CQ:json,data={json.dumps(card, ensure_ascii=False).replace(',', '&#44;')}]"
    await MessageService.process_incoming_message(
        db_session,
        "robot-card",
        "qq",
        {
            "room_id": "group-card",
            "message_type": "group",
            "sender_id": "user-card",
            "nickname": "Card User",
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
    payload = json.loads(html.unescape(messages[0].local_message.removeprefix("[CQ:json,data=").removesuffix("]")))
    detail = payload["meta"]["detail_1"]

    assert report.candidates == 1
    assert report.updated == 1
    assert report.failed == 0
    assert detail["url"] == "https://example.com/page"
    assert detail["local_page"].startswith("/static/storage/")


@pytest.mark.asyncio
async def test_backfill_historical_media_detects_json_escaped_card_urls(db_session, tmp_path):
    client = StubAsyncClient({})
    card = {"meta": {"detail_1": {"title": "Card", "preview": "http://media.local/preview.jpg", "url": "m.q.qq.com/a/s/card"}}}
    escaped_json = json.dumps(card, ensure_ascii=False).replace("/", "\\/").replace(",", "&#44;")
    raw = f"[CQ:json,data={escaped_json}]"
    await MessageService.process_incoming_message(
        db_session,
        "robot-card",
        "qq",
        {
            "room_id": "group-card",
            "message_type": "group",
            "sender_id": "user-card",
            "nickname": "Card User",
            "raw_message": raw,
            "timestamp": 1783000000,
        },
    )

    report = await MediaBackfillService.backfill_historical_media(
        db_session,
        http_client=client,
        storage_root=tmp_path,
        public_prefix="/static/storage",
        finalize_unavailable=True,
    )

    messages = await MessageService.list_messages(db_session)
    payload = json.loads(html.unescape(messages[0].local_message.removeprefix("[CQ:json,data=").removesuffix("]")))
    detail = payload["meta"]["detail_1"]

    assert report.candidates == 1
    assert report.updated == 1
    assert detail["preview"].startswith("/static/storage/")
    assert detail["local_page"].startswith("/static/storage/")


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
    assert payload["reason_summary"] == {}
    assert payload["failures"] == []
