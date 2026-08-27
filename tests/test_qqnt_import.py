from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.config import Settings, get_settings
from app.database import get_db_session
from app.import_contract import (
    can_transition_media_state,
    qqnt_external_message_id,
    validate_media_state_combination,
)
from app.main import app
from app.models import AuditLog, ImportBatch, MediaAsset, Message, MessageMediaReference, MessageSourceRecord
from app.schemas import ImportBatchCreateRequest, ImportBatchMessagesRequest, ImportSourceCreateRequest
from app.services.backup_service import BackupService
from app.services.import_service import ImportService
from app.services.media_backfill_service import MediaBackfillService
from app.services.message_service import MessageService
from app.services.offline_audit_service import OfflineAuditService
from app.services.query_service import QueryService
from tests.test_media_service import StubAsyncClient


def _message_item(
    *,
    message_id: str,
    source_key: str,
    source_state: str = "not_downloaded",
    archive_state: str = "metadata_only",
    asset_file_hash: str | None = None,
    thumbnail_file_hash: str | None = None,
    local_message: str | None = None,
    raw_marker: str = "v1",
    platform_message_id: str | None = None,
) -> dict:
    return {
        "message": {
            "robot_id": "123456789",
            "platform": "qq",
            "room_id": "987654321",
            "message_type": "group",
            "sender_id": "111222333",
            "nickname": "Alice",
            "raw_message": "[CQ:video,file=test.mp4]",
            "local_message": local_message or "[CQ:video,file=test.mp4]",
            "timestamp": 1784390000,
            "message_id": message_id,
        },
        "source_record": {
            "source_table": "group_msg_table",
            "source_key": source_key,
            "platform_message_id": platform_message_id,
            "schema_version": "qqnt-9.9-x",
            "raw_columns": {"marker": raw_marker},
        },
        "media": [
            {
                "ordinal": 0,
                "media_type": "video",
                "source_state": source_state,
                "archive_state": archive_state,
                "asset_file_hash": asset_file_hash,
                "thumbnail_file_hash": thumbnail_file_hash,
                "file_name": "test.mp4",
                "declared_file_size": 1024,
                "duration_ms": 26000,
                "source_md5": "source-md5",
                "source_uuid": "source-uuid",
            }
        ],
    }


def test_q0_contract_freezes_identity_and_monotonic_state_rules():
    message_id = qqnt_external_message_id(
        account_id="123456789",
        chat_type="group",
        conversation_id="987654321",
        msg_id=100,
        msg_random=200,
        msg_seq=300,
    )
    assert len(message_id) == 64
    assert message_id == qqnt_external_message_id(
        account_id="123456789",
        chat_type="group",
        conversation_id="987654321",
        msg_id=100,
        msg_random=200,
        msg_seq=300,
    )
    validate_media_state_combination("not_downloaded", "metadata_only")
    assert can_transition_media_state("not_downloaded", "metadata_only", "not_downloaded", "thumbnail_only") is True
    assert can_transition_media_state("not_downloaded", "thumbnail_only", "downloaded", "complete") is True
    assert can_transition_media_state("downloaded", "complete", "not_downloaded", "metadata_only") is False


def test_collector_import_openapi_contract_is_published():
    app.openapi_schema = None
    spec = app.openapi()
    assert "/api/import/sources" in spec["paths"]
    assert "/api/import/batches" in spec["paths"]
    assert "/api/import/batches/{batch_id}/messages" in spec["paths"]
    assert "/api/import/batches/{batch_id}/complete" in spec["paths"]
    assert "/api/import/batches/{batch_id}/fail" in spec["paths"]


async def test_import_service_replays_chunks_and_upgrades_media_without_duplicate_messages(db_session, tmp_path):
    source, created = await ImportService.upsert_source(
        db_session,
        ImportSourceCreateRequest(
            account_id="123456789",
            device_id="main-pc",
            device_name="Main-PC",
            qq_version="9.9.20",
            schema_version="qqnt-9.9-x",
        ),
    )
    assert created is True
    batch, created = await ImportService.create_batch(
        db_session,
        ImportBatchCreateRequest(id="batch-1", source_id=source.id, mode="initial"),
    )
    assert created is True

    message_id = qqnt_external_message_id(
        account_id="123456789",
        chat_type="group",
        conversation_id="987654321",
        msg_id=100,
        msg_random=200,
        msg_seq=300,
    )
    initial = ImportBatchMessagesRequest(messages=[_message_item(message_id=message_id, source_key="100:200:300")])
    first = await ImportService.import_batch_messages(db_session, batch_id=batch.id, payload=initial)
    replay = await ImportService.import_batch_messages(db_session, batch_id=batch.id, payload=initial)
    assert first.inserted == 1
    assert replay.replayed is True
    assert replay.inserted == 1

    message_count = await db_session.scalar(select(func.count(Message.msg_hash)))
    batch_model = await db_session.get(ImportBatch, batch.id)
    assert message_count == 1
    assert batch_model.scanned_messages == 1
    assert batch_model.inserted_messages == 1

    thumbnail = MediaAsset(
        file_hash="thumb-hash",
        file_type="image",
        file_size=12,
        local_path="/static/storage/thumb.jpg",
    )
    db_session.add(thumbnail)
    await db_session.commit()
    thumbnail_payload = ImportBatchMessagesRequest(
        messages=[
            _message_item(
                message_id=message_id,
                source_key="100:200:300",
                archive_state="thumbnail_only",
                thumbnail_file_hash="thumb-hash",
                raw_marker="v2",
            )
        ]
    )
    upgraded_thumbnail = await ImportService.import_batch_messages(db_session, batch_id=batch.id, payload=thumbnail_payload)
    assert upgraded_thumbnail.updated == 1

    video = MediaAsset(
        file_hash="video-hash",
        file_type="video",
        file_size=1024,
        local_path="/static/storage/video.mp4",
    )
    db_session.add(video)
    await db_session.commit()
    complete_payload = ImportBatchMessagesRequest(
        messages=[
            _message_item(
                message_id=message_id,
                source_key="100:200:300",
                source_state="downloaded",
                archive_state="complete",
                asset_file_hash="video-hash",
                thumbnail_file_hash="thumb-hash",
                local_message="[CQ:video,file=/static/storage/video.mp4]",
                raw_marker="v3",
            )
        ]
    )
    completed = await ImportService.import_batch_messages(db_session, batch_id=batch.id, payload=complete_payload)
    assert completed.updated == 1

    reference = (await db_session.execute(select(MessageMediaReference))).scalar_one()
    message = (await db_session.execute(select(Message))).scalar_one()
    assert (reference.source_state, reference.archive_state) == ("downloaded", "complete")
    assert reference.asset_file_hash == "video-hash"
    assert message.local_message == "[CQ:video,file=/static/storage/video.mp4]"
    assert await db_session.scalar(select(func.count(Message.msg_hash))) == 1

    stale_payload = ImportBatchMessagesRequest(
        messages=[_message_item(message_id=message_id, source_key="100:200:300", raw_marker="stale-replay")]
    )
    stale_payload.messages[0].media[0].failure_code = "STALE_FAILURE"
    stale_payload.messages[0].media[0].failure_detail = "must not contaminate a complete archive"
    await ImportService.import_batch_messages(db_session, batch_id=batch.id, payload=stale_payload)
    await db_session.refresh(reference)
    assert (reference.source_state, reference.archive_state) == ("downloaded", "complete")
    assert reference.asset_file_hash == "video-hash"
    assert reference.failure_code is None
    assert reference.failure_detail is None
    await db_session.refresh(batch_model)
    assert batch_model.uploaded_media == 1

    replacement = MediaAsset(
        file_hash="replacement-video-hash",
        file_type="video",
        file_size=2048,
        local_path="/static/storage/replacement-video.mp4",
    )
    db_session.add(replacement)
    await db_session.commit()
    replacement_payload = ImportBatchMessagesRequest(
        messages=[
            _message_item(
                message_id=message_id,
                source_key="100:200:300",
                source_state="downloaded",
                archive_state="complete",
                asset_file_hash="replacement-video-hash",
                local_message="[CQ:video,file=/static/storage/replacement-video.mp4]",
                raw_marker="v4",
            )
        ]
    )
    rebound = await ImportService.import_batch_messages(db_session, batch_id=batch.id, payload=replacement_payload)
    assert rebound.updated == 1
    await db_session.refresh(reference)
    assert reference.asset_file_hash == "replacement-video-hash"


async def test_qqnt_import_deduplicates_an_existing_napcat_message_by_platform_message_id(db_session):
    raw_platform_message_id = "762197037"
    napcat_hash = await MessageService.process_incoming_message(
        db_session,
        "123456789",
        "qq",
        {
            "room_id": "987654321",
            "message_type": "group",
            "sender_id": "111222333",
            "nickname": "Alice",
            "raw_message": "same physical message",
            "timestamp": 1784390000,
            "message_id": raw_platform_message_id,
        },
    )
    source, _ = await ImportService.upsert_source(
        db_session,
        ImportSourceCreateRequest(account_id="123456789", device_id="main-pc"),
    )
    batch, _ = await ImportService.create_batch(
        db_session,
        ImportBatchCreateRequest(id="cross-source", source_id=source.id, mode="reconcile"),
    )
    qqnt_message_id = qqnt_external_message_id(
        account_id="123456789",
        chat_type="group",
        conversation_id="987654321",
        msg_id=raw_platform_message_id,
        msg_random=200,
        msg_seq=300,
    )
    item = _message_item(
        message_id=qqnt_message_id,
        source_key="762197037:200:300",
        platform_message_id=raw_platform_message_id,
    )
    item["message"]["raw_message"] = "same physical message"
    item["message"]["local_message"] = "same physical message"
    item["media"] = []

    response = await ImportService.import_batch_messages(
        db_session,
        batch_id=batch.id,
        payload=ImportBatchMessagesRequest(messages=[item]),
    )

    assert response.updated == 1
    assert await db_session.scalar(select(func.count(Message.msg_hash))) == 1
    record = (await db_session.execute(select(MessageSourceRecord))).scalar_one()
    assert record.msg_hash == napcat_hash
    assert record.source_external_message_id == qqnt_message_id
    assert record.platform_message_id == raw_platform_message_id
    around = await QueryService.list_messages(
        db_session,
        robot_id="123456789",
        room_id="987654321",
        around_message_id=qqnt_message_id,
    )
    assert [message.msg_hash for message in around] == [napcat_hash]
    assert qqnt_message_id in around[0].external_message_aliases


async def test_cross_source_media_keeps_the_more_complete_napcat_archive(db_session):
    raw_platform_message_id = "napcat-video-id"
    db_session.add(
        MediaAsset(
            file_hash="napcat-video-asset",
            file_type="video",
            file_size=2048,
            local_path="/static/storage/napcat-video.mp4",
        )
    )
    await db_session.commit()
    napcat_hash = await MessageService.process_incoming_message(
        db_session,
        "123456789",
        "qq",
        {
            "room_id": "987654321",
            "message_type": "group",
            "sender_id": "111222333",
            "nickname": "Alice",
            "raw_message": "[CQ:video,file=test.mp4]",
            "local_message": "[CQ:video,file=/static/storage/napcat-video.mp4]",
            "timestamp": 1784390000,
            "message_id": raw_platform_message_id,
        },
    )
    source, _ = await ImportService.upsert_source(
        db_session,
        ImportSourceCreateRequest(account_id="123456789", device_id="main-pc"),
    )
    batch, _ = await ImportService.create_batch(
        db_session,
        ImportBatchCreateRequest(id="napcat-first-media", source_id=source.id, mode="reconcile"),
    )
    item = _message_item(
        message_id="qqnt-video-sha",
        source_key="napcat-video-source",
        platform_message_id=raw_platform_message_id,
    )

    await ImportService.import_batch_messages(
        db_session,
        batch_id=batch.id,
        payload=ImportBatchMessagesRequest(messages=[item]),
    )

    reference = (await db_session.execute(select(MessageMediaReference))).scalar_one()
    assert reference.msg_hash == napcat_hash
    assert (reference.source_state, reference.archive_state) == ("downloaded", "complete")
    assert reference.asset_file_hash == "napcat-video-asset"


async def test_napcat_after_qqnt_reuses_message_and_upgrades_structured_media(db_session):
    raw_platform_message_id = "napcat-after-qqnt"
    source, _ = await ImportService.upsert_source(
        db_session,
        ImportSourceCreateRequest(account_id="123456789", device_id="main-pc"),
    )
    batch, _ = await ImportService.create_batch(
        db_session,
        ImportBatchCreateRequest(id="qqnt-first-media", source_id=source.id, mode="initial"),
    )
    item = _message_item(
        message_id="qqnt-first-sha",
        source_key="qqnt-first-source",
        platform_message_id=raw_platform_message_id,
    )
    first = await ImportService.import_batch_messages(
        db_session,
        batch_id=batch.id,
        payload=ImportBatchMessagesRequest(messages=[item]),
    )
    msg_hash = first.items[0].msg_hash
    db_session.add(
        MediaAsset(
            file_hash="napcat-later-asset",
            file_type="video",
            file_size=4096,
            local_path="/static/storage/napcat-later.mp4",
        )
    )
    await db_session.commit()

    resolved_hash = await MessageService.process_incoming_message(
        db_session,
        "123456789",
        "qq",
        {
            "room_id": "987654321",
            "message_type": "group",
            "sender_id": "111222333",
            "nickname": "Alice",
            "raw_message": "[CQ:video,file=test.mp4]",
            "local_message": "[CQ:video,file=/static/storage/napcat-later.mp4]",
            "timestamp": 1784390000,
            "message_id": raw_platform_message_id,
        },
    )

    assert resolved_hash == msg_hash
    assert await db_session.scalar(select(func.count(Message.msg_hash))) == 1
    reference = (await db_session.execute(select(MessageMediaReference))).scalar_one()
    assert (reference.source_state, reference.archive_state) == ("downloaded", "complete")
    assert reference.asset_file_hash == "napcat-later-asset"


def test_collector_batch_rejects_messages_without_stable_message_id():
    item = _message_item(message_id="stable", source_key="source-key")
    item["message"]["message_id"] = None

    try:
        ImportBatchMessagesRequest(messages=[item])
    except ValueError as exc:
        assert "stable message.message_id" in str(exc)
    else:
        raise AssertionError("Collector batch message_id must be required")


async def test_not_downloaded_media_is_queryable_and_not_an_offline_error(db_session, tmp_path):
    source, _ = await ImportService.upsert_source(
        db_session,
        ImportSourceCreateRequest(account_id="123456789", device_id="main-pc", device_name="Main-PC"),
    )
    batch, _ = await ImportService.create_batch(
        db_session,
        ImportBatchCreateRequest(id="batch-query", source_id=source.id, mode="incremental"),
    )
    payload = ImportBatchMessagesRequest(messages=[_message_item(message_id="message-query", source_key="query-key")])
    await ImportService.import_batch_messages(db_session, batch_id=batch.id, payload=payload)

    report = await OfflineAuditService.audit_offline_readiness(
        db_session,
        robot_id="123456789",
        room_id="987654321",
        storage_root=tmp_path,
    )
    assert report.not_downloaded_media == 1
    assert report.not_downloaded_videos == 1
    assert report.missing_media_assets == 0
    assert report.offline_ready is False  # profile avatars are intentionally not cached in this fixture
    assert "media_reference_asset_missing" not in report.reason_summary

    settings = Settings(storage_root=tmp_path, public_storage_prefix="/static/storage")

    async def override_db_session():
        yield db_session

    app.dependency_overrides[get_db_session] = override_db_session
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(
                "/api/messages",
                params={
                    "robot_id": "123456789",
                    "room_id": "987654321",
                    "media_state": "not_downloaded",
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    item = response.json()[0]
    assert item["media"][0]["source_state"] == "not_downloaded"
    assert item["media"][0]["asset_local_path"] is None
    assert item["import_sources"][0]["device_name"] == "Main-PC"


async def test_batch_item_failure_uses_savepoint_and_marks_batch_partial(db_session):
    source, _ = await ImportService.upsert_source(
        db_session,
        ImportSourceCreateRequest(account_id="123456789", device_id="main-pc"),
    )
    batch, _ = await ImportService.create_batch(
        db_session,
        ImportBatchCreateRequest(id="partial-batch", source_id=source.id, mode="incremental"),
    )
    good = _message_item(message_id="good-message", source_key="good-key")
    bad = _message_item(
        message_id="bad-message",
        source_key="bad-key",
        source_state="downloaded",
        archive_state="complete",
        asset_file_hash="asset-does-not-exist",
    )

    response = await ImportService.import_batch_messages(
        db_session,
        batch_id=batch.id,
        payload=ImportBatchMessagesRequest(messages=[good, bad]),
    )

    assert response.inserted == 1
    assert response.failed == 1
    assert [item.status for item in response.items] == ["inserted", "failed"]
    assert await db_session.scalar(select(func.count(Message.msg_hash))) == 1
    await db_session.refresh(batch)
    assert batch.status == "partial"
    assert batch.scanned_messages == 2
    assert batch.failed_media == 1


async def test_batch_rejects_message_from_a_different_source_account(db_session):
    source, _ = await ImportService.upsert_source(
        db_session,
        ImportSourceCreateRequest(account_id="account-a", device_id="main-pc"),
    )
    batch, _ = await ImportService.create_batch(
        db_session,
        ImportBatchCreateRequest(id="wrong-account", source_id=source.id, mode="incremental"),
    )
    item = _message_item(message_id="wrong-account-message", source_key="wrong-account-key")

    response = await ImportService.import_batch_messages(
        db_session,
        batch_id=batch.id,
        payload=ImportBatchMessagesRequest(messages=[item]),
    )

    assert response.failed == 1
    assert "does not match the import source account_id" in response.items[0].error
    assert await db_session.scalar(select(func.count(Message.msg_hash))) == 0


async def test_fail_batch_status_takes_precedence_over_partial_flag(db_session):
    source, _ = await ImportService.upsert_source(
        db_session,
        ImportSourceCreateRequest(account_id="123456789", device_id="main-pc"),
    )
    batch, _ = await ImportService.create_batch(
        db_session,
        ImportBatchCreateRequest(id="failed-batch", source_id=source.id, mode="incremental"),
    )

    finished = await ImportService.finish_batch(
        db_session,
        batch_id=batch.id,
        failed=True,
        partial=True,
        detail={},
        error_code="DB_OPEN_FAILED",
    )

    assert finished.status == "failed"
    assert finished.completed_at is not None


async def test_structured_not_downloaded_media_never_uses_legacy_placeholder_backfill(db_session, tmp_path):
    source, _ = await ImportService.upsert_source(
        db_session,
        ImportSourceCreateRequest(account_id="123456789", device_id="main-pc"),
    )
    batch, _ = await ImportService.create_batch(
        db_session,
        ImportBatchCreateRequest(id="placeholder-boundary", source_id=source.id, mode="incremental"),
    )
    item = _message_item(message_id="placeholder-message", source_key="placeholder-key")
    remote_cq = "[CQ:video,file=test.mp4,url=https://media.invalid/test.mp4]"
    item["message"]["raw_message"] = remote_cq
    item["message"]["local_message"] = remote_cq
    await ImportService.import_batch_messages(
        db_session,
        batch_id=batch.id,
        payload=ImportBatchMessagesRequest(messages=[item]),
    )

    report = await MediaBackfillService.backfill_historical_media(
        db_session,
        http_client=StubAsyncClient({}),
        storage_root=tmp_path,
        public_prefix="/static/storage",
        finalize_unavailable=True,
    )

    message = (await db_session.execute(select(Message))).scalar_one()
    assert report.updated == 0
    assert message.local_message == remote_cq
    assert await db_session.scalar(select(func.count(MediaAsset.file_hash))) == 0


async def test_offline_audit_still_reports_uncovered_remote_media_in_mixed_message(db_session, tmp_path):
    source, _ = await ImportService.upsert_source(
        db_session,
        ImportSourceCreateRequest(account_id="123456789", device_id="main-pc"),
    )
    batch, _ = await ImportService.create_batch(
        db_session,
        ImportBatchCreateRequest(id="mixed-media-audit", source_id=source.id, mode="incremental"),
    )
    item = _message_item(message_id="mixed-media-message", source_key="mixed-media-key")
    mixed = (
        "[CQ:video,file=test.mp4,url=https://media.invalid/video.mp4]"
        "[CQ:image,file=extra.jpg,url=https://media.invalid/extra.jpg]"
    )
    item["message"]["raw_message"] = mixed
    item["message"]["local_message"] = mixed
    await ImportService.import_batch_messages(
        db_session,
        batch_id=batch.id,
        payload=ImportBatchMessagesRequest(messages=[item]),
    )

    report = await OfflineAuditService.audit_offline_readiness(
        db_session,
        robot_id="123456789",
        room_id="987654321",
        storage_root=tmp_path,
    )

    assert report.remote_media_urls == 1
    assert any(issue.target == "https://media.invalid/extra.jpg" for issue in report.issues)
    assert all(issue.target != "https://media.invalid/video.mp4" for issue in report.issues)


async def test_collector_import_api_is_idempotent_and_audited(db_session, tmp_path):
    settings = Settings(storage_root=tmp_path, public_storage_prefix="/static/storage")

    async def override_db_session():
        yield db_session

    app.dependency_overrides[get_db_session] = override_db_session
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            source_response = await client.post(
                "/api/import/sources",
                json={"account_id": "123456789", "device_id": "main-pc", "device_name": "Main-PC"},
            )
            source_id = source_response.json()["id"]
            batch_response = await client.post(
                "/api/import/batches",
                json={"id": "api-batch", "source_id": source_id, "mode": "incremental"},
            )
            payload = {"messages": [_message_item(message_id="api-message", source_key="api-source-key")]}
            first = await client.post("/api/import/batches/api-batch/messages", json=payload)
            replay = await client.post("/api/import/batches/api-batch/messages", json=payload)
            completed = await client.post("/api/import/batches/api-batch/complete", json={})
    finally:
        app.dependency_overrides.clear()

    assert source_response.status_code == 200
    assert batch_response.status_code == 200
    assert first.status_code == 200 and first.json()["inserted"] == 1
    assert replay.status_code == 200 and replay.json()["replayed"] is True
    assert completed.status_code == 200 and completed.json()["status"] == "completed"
    assert await db_session.scalar(select(func.count(Message.msg_hash))) == 1
    assert await db_session.scalar(select(func.count(MessageSourceRecord.id))) == 1
    logs = list((await db_session.execute(select(AuditLog).order_by(AuditLog.id.asc()))).scalars().all())
    assert {log.action for log in logs} >= {
        "import.source.upsert",
        "import.batch.create",
        "import.batch.messages",
        "import.batch.complete",
    }
    assert all("api-source-key" not in (log.detail_json or "") for log in logs)
    assert all("[CQ:video" not in (log.detail_json or "") for log in logs)


async def test_backup_v3_roundtrip_preserves_not_downloaded_reference_without_fake_asset(db_session):
    source, _ = await ImportService.upsert_source(
        db_session,
        ImportSourceCreateRequest(account_id="123456789", device_id="main-pc"),
    )
    batch, _ = await ImportService.create_batch(
        db_session,
        ImportBatchCreateRequest(id="backup-batch", source_id=source.id, mode="initial"),
    )
    await ImportService.import_batch_messages(
        db_session,
        batch_id=batch.id,
        payload=ImportBatchMessagesRequest(messages=[_message_item(message_id="backup-message", source_key="backup-key")]),
    )
    package = await BackupService.export_package(db_session, robot_id="123456789")

    assert package["manifest"]["schema"] == "chat-audit-core.backup.v3"
    assert len(package["message_media_references"]) == 1
    assert package["message_media_references"][0]["asset_file_hash"] is None
    assert package["media_assets"] == []

    result = await BackupService.import_package(db_session, package)
    assert result["message_media_references"] == 1
    assert await db_session.scalar(select(func.count(Message.msg_hash))) == 1
    assert await db_session.scalar(select(func.count(MessageMediaReference.id))) == 1
    assert await db_session.scalar(select(func.count(MediaAsset.file_hash))) == 0


def _empty_v2_backup_package() -> dict:
    return {
        "manifest": {"schema": "chat-audit-core.backup.v2"},
        "messages": [],
        "robot_messages": [],
        "media_assets": [],
        "media_files": [],
        "room_profiles": [],
        "user_profiles": [],
        "import_sources": [],
        "import_batches": [],
        "message_source_records": [],
        "message_media_references": [],
    }


def _oversized_integer_v2_package() -> dict:
    package = _empty_v2_backup_package()
    package["messages"] = [
        {
            "msg_hash": "oversized-message",
            "platform": "qq",
            "room_id": "room",
            "message_type": "group",
            "sender_id": "sender",
            "raw_message": "payload",
            "local_message": "payload",
            "timestamp": 2**100,
        }
    ]
    package["media_assets"] = [
        {
            "file_hash": "oversized-asset",
            "file_type": "video",
            "file_size": 2**100,
            "local_path": "/static/storage/oversized.mp4",
        }
    ]
    package["import_sources"] = [
        {
            "id": "oversized-source",
            "source_type": "qqnt_local_db",
            "platform": "qq",
            "account_id": "robot",
            "device_id": "device",
            "status": "active",
            "first_seen_at": "2026-07-18T00:00:00Z",
            "last_seen_at": "2026-07-18T00:00:00Z",
        }
    ]
    package["import_batches"] = [
        {
            "id": "oversized-batch",
            "source_id": "oversized-source",
            "mode": "initial",
            "status": "running",
            "started_at": "2026-07-18T00:00:00Z",
            "scanned_messages": 2**100,
        }
    ]
    package["message_media_references"] = [
        {
            "msg_hash": "oversized-message",
            "ordinal": 2**100,
            "media_type": "video",
            "source_state": "not_downloaded",
            "archive_state": "metadata_only",
            "width": 2**100,
            "first_seen_at": "2026-07-18T00:00:00Z",
            "last_checked_at": "2026-07-18T00:00:00Z",
        }
    ]
    return package


def test_backup_v2_requires_structured_import_sections():
    package = {
        "manifest": {"schema": "chat-audit-core.backup.v2"},
        "messages": [],
        "robot_messages": [],
        "media_assets": [],
        "media_files": [],
        "room_profiles": [],
        "user_profiles": [],
    }

    report = BackupService.validate_import_package(package)

    assert report["valid"] is False
    assert any("import_sources section is required" in error for error in report["errors"])
    assert any("message_media_references section is required" in error for error in report["errors"])


def test_backup_v2_rejects_invalid_media_state_and_empty_referenced_asset():
    package = {
        "manifest": {"schema": "chat-audit-core.backup.v2"},
        "messages": [
            {
                "msg_hash": "message-hash",
                "platform": "qq",
                "room_id": "room",
                "message_type": "group",
                "sender_id": "sender",
                "raw_message": "video",
                "local_message": "video",
                "timestamp": 1,
            }
        ],
        "robot_messages": [],
        "media_assets": [
            {"file_hash": "empty-asset", "file_type": "video", "file_size": 0, "local_path": "/static/storage/empty.mp4"}
        ],
        "media_files": [],
        "room_profiles": [],
        "user_profiles": [],
        "import_sources": [],
        "import_batches": [],
        "message_source_records": [],
        "message_media_references": [
            {
                "msg_hash": "message-hash",
                "ordinal": 0,
                "media_type": "video",
                "source_state": "downloaded",
                "archive_state": "metadata_only",
                "asset_file_hash": "empty-asset",
                "first_seen_at": "2026-07-18T00:00:00Z",
                "last_checked_at": "2026-07-18T00:00:00Z",
            }
        ],
    }

    report = BackupService.validate_import_package(package)

    assert report["valid"] is False
    assert any("violates the media state contract" in error for error in report["errors"])
    assert any("references an empty media asset" in error for error in report["errors"])


def test_backup_validation_handles_malformed_section_items_without_crashing():
    package = {
        "manifest": {"schema": "chat-audit-core.backup.v2"},
        "messages": 1,
        "robot_messages": [],
        "media_assets": [None],
        "media_files": ["bad"],
        "room_profiles": [],
        "user_profiles": [],
        "import_sources": [None],
        "import_batches": [],
        "message_source_records": [],
        "message_media_references": [None],
    }

    report = BackupService.validate_import_package(package)

    assert report["valid"] is False
    assert any("must be a list" in error or "must be an object" in error for error in report["errors"])

    malformed_manifest = BackupService.validate_import_package({"manifest": "bad"})
    assert malformed_manifest["valid"] is False
    assert "manifest must be an object" in malformed_manifest["errors"]

    malformed_schema = BackupService.validate_import_package({"manifest": {"schema": []}})
    assert malformed_schema["valid"] is False
    assert any("unsupported backup schema" in error for error in malformed_schema["errors"])


def test_backup_validation_handles_malformed_checksum_objects(tmp_path):
    package = {
        "manifest": {"schema": "chat-audit-core.backup.v1"},
        "messages": [],
        "robot_messages": [],
        "media_assets": [
            {
                "file_hash": "asset",
                "file_type": "image",
                "file_size": 1,
                "local_path": "/static/storage/asset.jpg",
                "file_checksum": "bad",
            }
        ],
        "media_files": [],
    }

    report = BackupService.validate_import_package(package, storage_root=tmp_path)

    assert report["valid"] is False
    assert any("invalid media checksum" in error for error in report["errors"])


def test_backup_validation_rejects_malformed_media_asset_types_with_valid_package_checksum(tmp_path):
    package = _empty_v2_backup_package()
    package["media_assets"] = [
        {
            "file_hash": "asset",
            "file_type": "image",
            "file_size": 1,
            "local_path": 123,
            "file_checksum": {"algorithm": "sha256", "value": "0" * 64},
        }
    ]
    BackupService.attach_package_checksum(package)

    report = BackupService.validate_import_package(package, storage_root=tmp_path)

    assert report["valid"] is False
    assert report["checksum_valid"] is True
    assert any("media_assets[0].local_path must be a str" in error for error in report["errors"])


def test_backup_validation_rejects_unhashable_cross_reference_fields_without_crashing():
    package = _empty_v2_backup_package()
    package["messages"] = [
        {
            "msg_hash": [],
            "platform": "qq",
            "room_id": "room",
            "message_type": "group",
            "sender_id": "sender",
            "raw_message": "payload",
            "local_message": "payload",
            "timestamp": 1,
        }
    ]
    package["import_sources"] = [
        {
            "id": {},
            "source_type": "qqnt_local_db",
            "platform": "qq",
            "account_id": "robot",
            "device_id": "device",
            "status": {},
            "first_seen_at": "2026-07-18T00:00:00Z",
            "last_seen_at": "2026-07-18T00:00:00Z",
        }
    ]
    package["import_batches"] = [
        {
            "id": [],
            "source_id": {},
            "mode": [],
            "status": {},
            "started_at": "2026-07-18T00:00:00Z",
        }
    ]

    report = BackupService.validate_import_package(package)

    assert report["valid"] is False
    assert any("must be a str" in error for error in report["errors"])


def test_backup_validation_rejects_integers_outside_the_portable_sql_range():
    report = BackupService.validate_import_package(_oversized_integer_v2_package())

    assert report["valid"] is False
    assert sum("must fit a signed 32-bit SQL integer" in error for error in report["errors"]) >= 5


def test_backup_validation_rejects_orphan_robot_message_associations():
    package = _empty_v2_backup_package()
    package["robot_messages"] = [{"robot_id": "robot", "msg_hash": "missing-message"}]

    report = BackupService.validate_import_package(package)

    assert report["valid"] is False
    assert any("robot_messages[0] references an unknown message" in error for error in report["errors"])


def test_collector_contract_rejects_oversized_sql_integer_fields():
    timestamp_item = _message_item(message_id="oversized-timestamp", source_key="oversized-timestamp-key")
    timestamp_item["message"]["timestamp"] = 2**100
    with pytest.raises(ValueError):
        ImportBatchMessagesRequest(messages=[timestamp_item])

    media_item = _message_item(message_id="oversized-media", source_key="oversized-media-key")
    media_item["media"][0]["width"] = 2**100
    with pytest.raises(ValueError):
        ImportBatchMessagesRequest(messages=[media_item])


async def test_backup_validate_api_returns_invalid_report_for_malformed_manifest_metadata(db_session, tmp_path):
    package = _empty_v2_backup_package()
    package["manifest"].update({"checksum": [], "signature": [], "source": "invalid"})
    oversized_package = _oversized_integer_v2_package()
    settings = Settings(
        storage_root=tmp_path,
        backup_root=tmp_path / "backups",
        public_storage_prefix="/static/storage",
    )

    async def override_db_session():
        yield db_session

    app.dependency_overrides[get_db_session] = override_db_session
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/import/validate", json=package)
            malformed_schema_response = await client.post(
                "/api/import/validate",
                json={"manifest": {"schema": []}},
            )
            oversized_validate_response = await client.post(
                "/api/import/validate",
                json=oversized_package,
            )
            oversized_import_response = await client.post(
                "/api/import",
                json=oversized_package,
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    report = response.json()
    assert report["valid"] is False
    assert report["diff"] == {}
    assert report["source"] is None
    assert any("checksum must be an object" in error for error in report["errors"])
    assert any("signature must be an object" in error for error in report["errors"])
    assert any("manifest.source must be an object" in error for error in report["errors"])
    assert malformed_schema_response.status_code == 200
    malformed_schema_report = malformed_schema_response.json()
    assert malformed_schema_report["valid"] is False
    assert malformed_schema_report["schema"] is None
    assert malformed_schema_report["diff"] == {}
    assert oversized_validate_response.status_code == 200
    oversized_report = oversized_validate_response.json()
    assert oversized_report["valid"] is False
    assert oversized_report["diff"] == {}
    assert oversized_import_response.status_code == 400


async def test_backup_import_rejects_source_identity_collision_on_existing_id(db_session, tmp_path):
    first, _ = await ImportService.upsert_source(
        db_session,
        ImportSourceCreateRequest(account_id="first-account", device_id="first-device"),
    )
    second, _ = await ImportService.upsert_source(
        db_session,
        ImportSourceCreateRequest(account_id="second-account", device_id="second-device"),
    )
    package = _empty_v2_backup_package()
    package["import_sources"] = [
        {
            "id": first.id,
            "source_type": second.source_type,
            "platform": second.platform,
            "account_id": second.account_id,
            "device_id": second.device_id,
            "status": "active",
            "first_seen_at": "2026-07-18T00:00:00Z",
            "last_seen_at": "2026-07-18T00:00:00Z",
        }
    ]

    preview = await BackupService.preview_import_package(db_session, package)
    assert preview["valid"] is False
    assert any("conflicts with an existing source identity" in error for error in preview["errors"])

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
    await db_session.refresh(first)
    assert (first.account_id, first.device_id) == ("first-account", "first-device")


async def test_backup_v1_ignores_v2_extension_sections(db_session):
    package = {
        "manifest": {"schema": "chat-audit-core.backup.v1"},
        "messages": [],
        "robot_messages": [],
        "media_assets": [],
        "import_sources": [{}],
        "import_batches": [{}],
        "message_source_records": [{}],
        "message_media_references": [{}],
    }

    validation = BackupService.validate_import_package(package)
    result = await BackupService.import_package(db_session, package)

    assert validation["valid"] is True
    assert validation["counts"]["import_sources"] == 0
    assert result == {"messages": 0, "robot_messages": 0, "media_assets": 0}


async def test_failed_backup_import_rolls_back_database_changes_before_audit(db_session, tmp_path):
    source, _ = await ImportService.upsert_source(
        db_session,
        ImportSourceCreateRequest(account_id="123456789", device_id="main-pc", device_name="Original Device"),
    )
    source_id = source.id
    batch, _ = await ImportService.create_batch(
        db_session,
        ImportBatchCreateRequest(id="rollback-batch", source_id=source.id, mode="initial"),
    )
    await ImportService.import_batch_messages(
        db_session,
        batch_id=batch.id,
        payload=ImportBatchMessagesRequest(messages=[_message_item(message_id="rollback-message", source_key="rollback-key")]),
    )
    package = await BackupService.export_package(db_session, robot_id="123456789")
    package["manifest"].pop("checksum", None)
    package["manifest"].pop("signature", None)
    package["import_sources"][0]["device_name"] = "Should Roll Back"
    other_message = dict(package["messages"][0])
    other_message["msg_hash"] = "other-message-hash"
    other_message["external_message_id"] = "other-message-id"
    package["messages"].append(other_message)
    package["message_source_records"][0]["msg_hash"] = "other-message-hash"

    settings = Settings(
        storage_root=tmp_path / "storage",
        backup_root=tmp_path / "backups",
        public_storage_prefix="/static/storage",
    )

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
    restored_source = await db_session.get(type(source), source_id)
    assert restored_source.device_name == "Original Device"
    assert await db_session.get(Message, "other-message-hash") is None


@pytest.mark.asyncio
async def test_same_qq_message_from_two_devices_is_deduplicated(db_session):
    source_a, _ = await ImportService.upsert_source(
        db_session,
        ImportSourceCreateRequest(account_id="123456789", device_id="home-pc", device_name="Home"),
    )
    source_b, _ = await ImportService.upsert_source(
        db_session,
        ImportSourceCreateRequest(account_id="123456789", device_id="office-pc", device_name="Office"),
    )
    batch_a, _ = await ImportService.create_batch(
        db_session, ImportBatchCreateRequest(id="two-device-a", source_id=source_a.id, mode="incremental")
    )
    batch_b, _ = await ImportService.create_batch(
        db_session, ImportBatchCreateRequest(id="two-device-b", source_id=source_b.id, mode="incremental")
    )
    item_a = _message_item(message_id="stable-qq-message", source_key="home-row", source_state="not_downloaded", archive_state="metadata_only")
    item_b = _message_item(message_id="stable-qq-message", source_key="office-row", source_state="not_downloaded", archive_state="metadata_only")

    result_a = await ImportService.import_batch_messages(
        db_session, batch_id=batch_a.id, payload=ImportBatchMessagesRequest(messages=[item_a])
    )
    result_b = await ImportService.import_batch_messages(
        db_session, batch_id=batch_b.id, payload=ImportBatchMessagesRequest(messages=[item_b])
    )

    assert result_a.inserted == 1
    assert result_b.updated == 1
    assert await db_session.scalar(select(func.count(Message.msg_hash))) == 1
    assert await db_session.scalar(select(func.count(MessageSourceRecord.id))) == 2


@pytest.mark.asyncio
async def test_reimporting_identical_message_reports_no_update(db_session):
    # Counting every re-import as an update made the batch report look like the
    # archive kept changing, which hides the batches that really did change
    # something. A second batch is not a replay, so it reaches the update branch.
    source, _ = await ImportService.upsert_source(
        db_session,
        ImportSourceCreateRequest(account_id="123456789", device_id="main-pc"),
    )
    item = _message_item(message_id="idempotent-message", source_key="idempotent-key")
    item["message"]["is_outgoing"] = True

    first_batch, _ = await ImportService.create_batch(
        db_session,
        ImportBatchCreateRequest(id="idempotent-batch-1", source_id=source.id, mode="initial"),
    )
    first = await ImportService.import_batch_messages(
        db_session,
        batch_id=first_batch.id,
        payload=ImportBatchMessagesRequest(messages=[item]),
    )

    second_batch, _ = await ImportService.create_batch(
        db_session,
        ImportBatchCreateRequest(id="idempotent-batch-2", source_id=source.id, mode="incremental"),
    )
    second = await ImportService.import_batch_messages(
        db_session,
        batch_id=second_batch.id,
        payload=ImportBatchMessagesRequest(messages=[item]),
    )

    assert first.inserted == 1
    assert second.inserted == 0
    assert second.updated == 0
    assert await db_session.scalar(select(func.count(Message.msg_hash))) == 1
