from __future__ import annotations

import json
import sqlite3

import httpx
from httpx import ASGITransport
from sqlalchemy import func, select

from app.config import Settings, get_settings
from app.database import get_db_session
from app.main import app
from app.models import ImportSource, Message, MessageMediaReference, MessageSourceRecord
from collector.app.config import load_config
from collector.media.staging import MediaStagingStore
from collector.sync.queue import CollectorQueue
from collector.sync.scheduler import CollectorScheduler
from collector.sync.scanner import QQNTScanner
from collector.sync.state import CollectorStateStore
from collector.sync.uploader import CollectorApiClient, QueueUploader


def _write_config(tmp_path):
    path = tmp_path / "collector.toml"
    root = str(tmp_path / "collector-data").replace("\\", "\\\\")
    path.write_text(
        f"""
[collector]
account_id = "123456789"
device_id = "test-device"
device_name = "Test PC"
data_dir = "{root}"
sync_interval_minutes = 1
reconcile_interval_hours = 1
staging_max_gb = 1
initial_import_batch_size = 20
incremental_batch_size = 10

[qq]
read_mode = "direct_readonly"
enable_vss_fallback = false

[server]
base_url = "http://collector.test"
request_timeout_seconds = 10
verify_tls = false

[media]
copy_before_upload = true
validate_video_with_ffprobe = false
upload_thumbnails = true
rescan_not_downloaded_days = 30
""".strip(),
        encoding="utf-8",
    )
    return load_config(path)


def _payload():
    return {
        "message": {
            "robot_id": "123456789",
            "platform": "qq",
            "room_id": "room",
            "message_type": "group",
            "sender_id": "sender",
            "raw_message": "video",
            "local_message": "video",
            "timestamp": 1783000000,
            "message_id": "collector-message-id",
        },
        "source_record": {
            "source_table": "message",
            "source_key": "source-key",
            "schema_version": "simulation-v1",
        },
        "media": [
            {
                "ordinal": 0,
                "media_type": "video",
                "source_state": "not_downloaded",
                "archive_state": "metadata_only",
            }
        ],
    }


async def test_collector_two_phase_upload_and_batch_completion(tmp_path):
    config = _write_config(tmp_path)
    store = CollectorStateStore(config.paths.state_db)
    store.initialize()
    queue = CollectorQueue(store)
    captured_message_payloads = []
    media_content = b"collector-video"

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer operator-token"
        if request.url.path == "/api/import/sources":
            return httpx.Response(200, json={"id": "server-source"})
        if request.url.path == "/api/external/media":
            assert media_content in await request.aread()
            return httpx.Response(
                201,
                json={
                    "local_path": "/static/storage/video.mp4",
                    "media_type": "video",
                    "file_name": "video.mp4",
                    "file_size": len(media_content),
                    "file_hash": __import__("hashlib").md5(media_content).hexdigest(),
                },
            )
        if request.url.path == "/api/import/batches":
            body = json.loads(await request.aread())
            return httpx.Response(200, json={"id": body["id"], "status": "running"})
        if request.url.path.endswith("/messages"):
            body = json.loads(await request.aread())
            captured_message_payloads.extend(body["messages"])
            return httpx.Response(
                200,
                json={
                    "inserted": 1,
                    "updated": 0,
                    "unchanged": 0,
                    "failed": 0,
                    "replayed": False,
                    "items": [{"index": 0, "message_id": "collector-message-id", "status": "inserted"}],
                },
            )
        if request.url.path.endswith("/complete"):
            return httpx.Response(200, json={"status": "completed"})
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url=config.server.base_url) as http_client:
        async with CollectorApiClient(config, lambda: "operator-token", client=http_client) as api:
            uploader = QueueUploader(config, store, queue, api)
            uploader.bootstrap_local_source()
            message_queue_id = queue.enqueue_message(
                source_id=uploader.local_source_id,
                dedupe_key="collector-message",
                payload=_payload(),
            )
            source_file = tmp_path / "video.mp4"
            source_file.write_bytes(media_content)
            staged = MediaStagingStore(config.paths.staging_dir, max_bytes=1024).stage(source_file)
            queue.enqueue_media(
                source_id=uploader.local_source_id,
                message_queue_id=message_queue_id,
                ordinal=0,
                media_type="video",
                staging_path=str(staged.path),
                file_hash=staged.file_hash,
                file_size=staged.file_size,
                content_sha256=staged.content_sha256,
                file_name="video.mp4",
            )
            result = await CollectorScheduler(config, store, uploader).run_once("incremental")

    assert result.status == "completed"
    assert queue.counts()["media"] == {"completed": 1}
    assert queue.counts()["messages"] == {"completed": 1}
    reference = captured_message_payloads[0]["media"][0]
    assert reference["source_state"] == "downloaded"
    assert reference["archive_state"] == "complete"
    assert reference["asset_file_hash"] == staged.file_hash
    assert store.status_snapshot()["last_run"]["status"] == "completed"


async def test_terminal_batch_gets_replaced_before_message_upload(tmp_path):
    config = _write_config(tmp_path)
    store = CollectorStateStore(config.paths.state_db)
    store.initialize()
    queue = CollectorQueue(store)
    create_calls = []
    message_batch_ids = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/import/sources":
            return httpx.Response(200, json={"id": "server-source"})
        if request.url.path == "/api/import/batches":
            body = json.loads(await request.aread())
            create_calls.append(body["id"])
            if len(create_calls) == 1:
                return httpx.Response(200, json={"id": body["id"], "status": "completed"})
            return httpx.Response(200, json={"id": body["id"], "status": "running"})
        if request.url.path.endswith("/messages"):
            message_batch_ids.append(request.url.path.split("/")[4])
            return httpx.Response(
                200,
                json={
                    "inserted": 1,
                    "updated": 0,
                    "unchanged": 0,
                    "failed": 0,
                    "replayed": False,
                    "items": [{"index": 0, "message_id": "collector-message-id", "status": "inserted"}],
                },
            )
        if request.url.path.endswith("/complete"):
            return httpx.Response(200, json={"status": "completed"})
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url=config.server.base_url) as http_client:
        async with CollectorApiClient(config, lambda: "operator-token", client=http_client) as api:
            uploader = QueueUploader(config, store, queue, api)
            uploader.bootstrap_local_source()
            queue.enqueue_message(
                source_id=uploader.local_source_id,
                dedupe_key="terminal-batch-retry",
                payload=_payload(),
            )
            result = await CollectorScheduler(config, store, uploader).run_once("incremental")

    assert result.status == "completed"
    assert len(create_calls) == 2
    assert create_calls[0] != create_calls[1]
    assert message_batch_ids == [create_calls[1]]
    assert queue.counts()["messages"] == {"completed": 1}


async def test_collector_simulated_message_reaches_real_import_api(db_session, tmp_path):
    config = _write_config(tmp_path)
    store = CollectorStateStore(config.paths.state_db)
    store.initialize()
    queue = CollectorQueue(store)
    settings = Settings(
        admin_api_token="operator-token",
        storage_root=tmp_path / "server-storage",
        backup_root=tmp_path / "server-backups",
    )

    async def override_db_session():
        yield db_session

    app.dependency_overrides[get_db_session] = override_db_session
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url=config.server.base_url) as http_client:
            async with CollectorApiClient(config, lambda: "operator-token", client=http_client) as api:
                uploader = QueueUploader(config, store, queue, api)
                uploader.bootstrap_local_source()
                queue.enqueue_message(
                    source_id=uploader.local_source_id,
                    dedupe_key="collector-real-api-message",
                    payload=_payload(),
                )
                result = await CollectorScheduler(config, store, uploader).run_once("incremental")
    finally:
        app.dependency_overrides.clear()

    assert result.status == "completed"
    assert await db_session.scalar(select(func.count(Message.msg_hash))) == 1
    assert await db_session.scalar(select(func.count(MessageMediaReference.id))) == 1
    assert await db_session.scalar(select(func.count(ImportSource.id))) == 1


async def test_parsed_qqnt_database_message_reaches_real_import_api(db_session, tmp_path):
    config = _write_config(tmp_path)
    database_path = tmp_path / "nt_msg.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE group_msg_001 (
                msgId TEXT NOT NULL, msgSeq INTEGER NOT NULL, msgTime INTEGER NOT NULL,
                peerUid TEXT NOT NULL, senderUid TEXT NOT NULL, msgContent TEXT NOT NULL,
                chatType TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO group_msg_001 VALUES(?,?,?,?,?,?,?)",
            (
                "qqnt-message-1",
                1,
                1783000000000,
                "group-a",
                "user-a",
                '{"elements":[{"type":"text","text":"parsed from QQNT"}]}',
                "group",
            ),
        )
    store = CollectorStateStore(config.paths.state_db)
    store.initialize()
    queue = CollectorQueue(store)
    settings = Settings(
        admin_api_token="operator-token",
        storage_root=tmp_path / "server-storage",
        backup_root=tmp_path / "server-backups",
    )

    async def override_db_session():
        yield db_session

    app.dependency_overrides[get_db_session] = override_db_session
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url=config.server.base_url) as http_client:
            async with CollectorApiClient(config, lambda: "operator-token", client=http_client) as api:
                uploader = QueueUploader(config, store, queue, api)
                uploader.bootstrap_local_source()
                scanner = QQNTScanner(
                    config,
                    store,
                    queue,
                    source_id=uploader.local_source_id,
                )
                scanner.scan = lambda **kwargs: QQNTScanner.scan(
                    scanner,
                    database_paths=[database_path],
                    **kwargs,
                )
                result = await CollectorScheduler(config, store, uploader, scanner).run_once("initial")
    finally:
        app.dependency_overrides.clear()

    assert result.stats["scan"]["scanned_messages"] == 1
    assert result.stats["scan"]["parser_failures"] == 0
    assert store.get_meta("initial_scan_completed") == "1"
    assert result.status == "completed"
    message = (await db_session.execute(select(Message))).scalar_one()
    source_record = (await db_session.execute(select(MessageSourceRecord))).scalar_one()
    assert message.raw_message == "parsed from QQNT"
    assert message.timestamp == 1783000000
    assert source_record.source_table == "group_msg_001"
    assert source_record.platform_message_id == "qqnt-message-1"


async def test_server_outage_keeps_queue_durable_for_next_restart(tmp_path):
    config = _write_config(tmp_path)
    store = CollectorStateStore(config.paths.state_db)
    store.initialize()
    queue = CollectorQueue(store)
    uploader = QueueUploader(config, store, queue, None)
    uploader.bootstrap_local_source()
    queue.enqueue_message(
        source_id=uploader.local_source_id,
        dedupe_key="durable-during-outage",
        payload=_payload(),
    )

    async def unavailable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("NAS offline", request=request)

    transport = httpx.MockTransport(unavailable)
    async with httpx.AsyncClient(transport=transport, base_url=config.server.base_url) as http_client:
        async with CollectorApiClient(config, lambda: "operator-token", client=http_client) as api:
            uploader.api = api
            result = await CollectorScheduler(config, store, uploader).run_once("incremental")

    restarted_store = CollectorStateStore(config.paths.state_db)
    restarted_store.initialize()
    assert result.status == "failed"
    assert result.error_code == "SERVER_UNAVAILABLE"
    assert CollectorQueue(restarted_store).counts()["messages"] == {"pending": 1}
