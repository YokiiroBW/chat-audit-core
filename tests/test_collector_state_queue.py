from __future__ import annotations

import hashlib
import time

import pytest

from collector.media.staging import MediaStagingStore, StagingError, StagingFullError
from collector.sync.queue import CollectorQueue
from collector.sync.state import CollectorStateStore


def _store_and_queue(tmp_path):
    store = CollectorStateStore(tmp_path / "collector_state.sqlite3")
    store.initialize()
    store.upsert_source(
        source_id="source-local",
        source_type="qqnt_local_db",
        account_id="123",
        device_id="pc",
    )
    return store, CollectorQueue(store)


def _message_payload():
    return {
        "message": {
            "robot_id": "123",
            "platform": "qq",
            "room_id": "room",
            "message_type": "group",
            "sender_id": "sender",
            "raw_message": "video",
            "local_message": "video",
            "timestamp": 1783000000,
            "message_id": "message-id",
        },
        "source_record": {"source_table": "message", "source_key": "1:2:3"},
        "media": [
            {
                "ordinal": 0,
                "media_type": "video",
                "source_state": "not_downloaded",
                "archive_state": "metadata_only",
            }
        ],
    }


def test_collector_queue_blocks_message_until_media_is_confirmed(tmp_path):
    store, queue = _store_and_queue(tmp_path)
    message_id = queue.enqueue_message(source_id="source-local", dedupe_key="message-1", payload=_message_payload())
    duplicate_id = queue.enqueue_message(source_id="source-local", dedupe_key="message-1", payload=_message_payload())
    assert duplicate_id == message_id

    media_id = queue.enqueue_media(
        source_id="source-local",
        message_queue_id=message_id,
        ordinal=0,
        media_type="video",
        staging_path=str(tmp_path / "video.mp4"),
        file_hash="md5-hash",
        file_size=10,
        content_sha256="sha256-hash",
    )

    assert queue.lease_ready("message", limit=10) == []
    media_item = queue.lease_ready("media", limit=1)[0]
    assert media_item.id == media_id
    queue.patch_message_media(
        message_id,
        0,
        {"file_hash": "md5-hash", "file_size": 10, "local_path": "/static/storage/video.mp4"},
    )
    queue.mark_completed(media_item, {"file_hash": "md5-hash"})
    assert queue.enqueue_media(
        source_id="source-local",
        message_queue_id=message_id,
        ordinal=0,
        media_type="video",
        staging_path=str(tmp_path / "replacement.mp4"),
        file_hash="replacement-hash",
        file_size=20,
        content_sha256="replacement-sha256",
    ) == media_id

    message_item = queue.lease_ready("message", limit=1)[0]
    reference = message_item.payload["media"][0]
    assert reference["source_state"] == "downloaded"
    assert reference["archive_state"] == "complete"
    assert reference["asset_file_hash"] == "md5-hash"
    queue.mark_completed(message_item, {"batch_id": "batch"})
    with store.connect() as connection:
        stored_media = connection.execute("SELECT file_hash,staging_path FROM pending_media WHERE id=?", (media_id,)).fetchone()
    assert stored_media["file_hash"] == "md5-hash"
    assert stored_media["staging_path"].endswith("video.mp4")
    assert queue.counts()["messages"] == {"completed": 1}
    assert queue.counts()["media"] == {"completed": 1}


def test_collector_queue_retry_and_dead_letter(tmp_path):
    _, queue = _store_and_queue(tmp_path)
    queue_id = queue.enqueue_message(source_id="source-local", dedupe_key="message-retry", payload=_message_payload())
    item = queue.lease_ready("message", limit=1)[0]

    assert queue.mark_retry(item, "SERVER_UNAVAILABLE", max_attempts=1) == "dead_letter"
    assert queue.counts()["messages"] == {"dead_letter": 1}
    assert queue.requeue_dead_letter("message", queue_id) is True
    assert queue.counts()["messages"] == {"pending": 1}


def test_media_staging_copies_real_file_and_enforces_quota(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"real-video-content")
    staging = MediaStagingStore(tmp_path / "staging", max_bytes=1024)

    staged = staging.stage(source)

    assert staged.path.read_bytes() == b"real-video-content"
    assert staged.file_hash == hashlib.md5(b"real-video-content").hexdigest()
    assert staged.content_sha256 == hashlib.sha256(b"real-video-content").hexdigest()
    assert source.read_bytes() == b"real-video-content"

    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    with pytest.raises(StagingError, match="zero-byte"):
        staging.stage(empty)

    too_large = tmp_path / "large.bin"
    too_large.write_bytes(b"x" * 2048)
    with pytest.raises(StagingFullError, match="MEDIA_STAGING_FULL"):
        staging.stage(too_large)

    old_time = time.time() - 90000
    staged.path.touch()
    import os

    os.utime(staged.path, (old_time, old_time))
    assert staging.delete_confirmed(staged.path) is True
