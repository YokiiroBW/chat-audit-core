from __future__ import annotations

import base64
import hashlib
import json

from collector.media.resolver import MediaResolver
from collector.media.staging import MediaStagingStore
from collector.parsers.message import MessageParserContext, enqueue_parsed_message, parse_source_message
from collector.qqnt.versions.base import SourceMessageRow
from collector.sync.queue import CollectorQueue
from collector.sync.state import CollectorStateStore


def _row(content, *, msg_id="100", source_key="row-1", table="group_msg_001", raw=None):
    raw_columns = {
        "msgId": msg_id,
        "msgSeq": 7,
        "msgTime": 1783000000000,
        "peerUid": "group-a",
        "senderUid": "user-a",
        "msgContent": content,
        "chatType": "group",
        "msgRandom": 42,
        "nickname": "Alice",
    }
    raw_columns.update(raw or {})
    return SourceMessageRow(
        source_table=table,
        source_primary_key=source_key,
        msg_time=1783000000000,
        msg_seq=7,
        msg_id=msg_id,
        conversation_id="group-a",
        sender_id="user-a",
        chat_type="group",
        content=content,
        raw_columns=raw_columns,
    )


def _context():
    return MessageParserContext(account_id="123456789", qq_version="9.9.test", schema_version="fixture-v1")


def _queue(tmp_path):
    store = CollectorStateStore(tmp_path / "state.sqlite3")
    store.initialize()
    store.upsert_source(
        source_id="source",
        source_type="qqnt_local_db",
        account_id="123456789",
        device_id="pc",
    )
    return store, CollectorQueue(store)


def test_message_parser_normalizes_text_face_reply_and_cards():
    content = {
        "elements": [
            {"type": "text", "text": "hello"},
            {"type": "face", "id": 14, "description": "smile"},
            {"type": "reply", "message_id": "quoted-id", "preview": "quoted"},
            {"type": "json", "data": {"meta": {"title": "card"}}},
            {"type": "xml", "data": "<msg action='view'/>"},
        ]
    }

    parsed = parse_source_message(_row(content), _context())

    raw = parsed.item["message"]["raw_message"]
    identity = "qqnt:123456789:group:group-a:100:42:7"
    assert raw.startswith("hello[CQ:face,id=14,name=smile][CQ:reply,id=quoted-id]quoted")
    assert "[CQ:json,data=" in raw
    assert "[CQ:xml,data=" in raw
    assert parsed.item["message"]["timestamp"] == 1783000000
    assert parsed.item["message"]["message_id"] == hashlib.sha256(identity.encode()).hexdigest()
    assert parsed.item["source_record"]["metadata"]["source_identity"] == identity
    assert parsed.failures == ()


def test_message_parser_attaches_qqnt_profile_metadata():
    context = MessageParserContext(
        account_id="123456789",
        sender_nicknames={"user-a": "????"},
        room_names={"group-a": "????"},
        sender_avatars={"user-a": "https://example.test/user.jpg"},
        room_avatars={"group-a": "https://example.test/group.jpg"},
    )

    parsed = parse_source_message(_row({"elements": [{"type": "text", "text": "hello"}]}), context)

    assert parsed.item["message"]["nickname"] == "Alice"
    metadata = parsed.item["source_record"]["metadata"]
    assert metadata["sender_avatar_url"] == "https://example.test/user.jpg"
    assert metadata["room_avatar_url"] == "https://example.test/group.jpg"


def test_video_state_parser_distinguishes_not_downloaded_missing_and_complete(tmp_path):
    not_downloaded = parse_source_message(
        _row({"elements": [{"type": "video", "file_name": "never.mp4", "uuid": "video-uuid"}]}),
        _context(),
    )
    reference = not_downloaded.item["media"][0]
    assert (reference["source_state"], reference["archive_state"]) == ("not_downloaded", "metadata_only")
    assert not_downloaded.uploads == ()

    missing = parse_source_message(
        _row(
            {"elements": [{"type": "video", "file_name": "missing.mp4", "local_path": str(tmp_path / "missing.mp4")}]},
            msg_id="101",
            source_key="row-2",
        ),
        _context(),
    )
    reference = missing.item["media"][0]
    assert (reference["source_state"], reference["archive_state"]) == ("missing", "metadata_only")

    video_path = tmp_path / "complete.mp4"
    video_path.write_bytes(b"complete-video")
    complete = parse_source_message(
        _row(
            {
                "elements": [
                    {
                        "type": "video",
                        "file_name": "complete.mp4",
                        "local_path": str(video_path),
                        "file_size": len(b"complete-video"),
                        "md5": hashlib.md5(b"complete-video").hexdigest(),
                    }
                ]
            },
            msg_id="102",
            source_key="row-3",
        ),
        _context(),
    )
    reference = complete.item["media"][0]
    assert (reference["source_state"], reference["archive_state"]) == ("downloaded", "complete")
    assert complete.uploads[0].path == video_path.resolve()


def test_enqueue_parser_stages_complete_and_thumbnail_media_before_message(tmp_path):
    full_path = tmp_path / "full.mp4"
    full_path.write_bytes(b"full-video")
    thumbnail_path = tmp_path / "thumb.jpg"
    thumbnail_path.write_bytes(b"thumbnail")
    content = {
        "elements": [
            {"type": "video", "file_name": "full.mp4", "local_path": str(full_path)},
            {"type": "video", "file_name": "thumb-only.mp4", "uuid": "thumb-video", "thumbnail_path": str(thumbnail_path)},
        ]
    }
    parsed = parse_source_message(_row(content), _context())
    store, queue = _queue(tmp_path)
    staging = MediaStagingStore(tmp_path / "staging", max_bytes=1024)

    message_queue_id = enqueue_parsed_message(parsed, source_id="source", queue=queue, store=store, staging=staging)

    assert queue.lease_ready("message", limit=1) == []
    media_items = queue.lease_ready("media", limit=10)
    assert {(item.ordinal, item.media_role) for item in media_items} == {(0, "asset"), (1, "thumbnail")}
    for item in media_items:
        uploaded = {"file_hash": item.file_hash, "file_size": item.file_size, "local_path": "/static/storage/file"}
        queue.patch_message_media(message_queue_id, item.ordinal, uploaded, media_role=item.media_role or "asset")
        queue.mark_completed(item, uploaded)
    message = queue.lease_ready("message", limit=1)[0]
    first, second = message.payload["media"]
    assert (first["source_state"], first["archive_state"], first["asset_file_hash"]) == (
        "downloaded",
        "complete",
        media_items[0].file_hash,
    )
    assert second["archive_state"] == "thumbnail_only"
    assert second["thumbnail_file_hash"]


def test_forward_payload_is_staged_as_real_json_and_patched_into_message(tmp_path):
    content = {
        "elements": [
            {
                "type": "forward",
                "forward_id": "forward-1",
                "nodes": [{"sender_id": "a", "timestamp": 1, "raw_message": "hello"}],
            }
        ]
    }
    parsed = parse_source_message(_row(content), _context())
    assert parsed.artifacts[0].content.startswith(b'{"forward_id":"forward-1"')
    store, queue = _queue(tmp_path)
    staging = MediaStagingStore(tmp_path / "staging", max_bytes=4096)

    message_queue_id = enqueue_parsed_message(parsed, source_id="source", queue=queue, store=store, staging=staging)
    artifact_item = queue.lease_ready("media", limit=1)[0]
    assert artifact_item.media_role == "artifact"
    uploaded = {
        "file_hash": artifact_item.file_hash,
        "file_size": artifact_item.file_size,
        "local_path": "/static/storage/forward.json",
        "replace_token": artifact_item.payload["replace_token"],
        "replacement": artifact_item.payload["replacement_template"].format(
            local_path="/static/storage/forward.json"
        ),
    }
    queue.patch_message_media(message_queue_id, artifact_item.ordinal or 0, uploaded, media_role="artifact")
    queue.mark_completed(artifact_item, uploaded)
    message = queue.lease_ready("message", limit=1)[0]
    assert message.payload["message"]["local_message"] == (
        "[CQ:forward,id=forward-1,local=/static/storage/forward.json]"
    )
    assert message.payload["media"] == []



def _varint(value: int) -> bytes:
    output = bytearray()
    while value >= 0x80:
        output.append((value & 0x7F) | 0x80)
        value >>= 7
    output.append(value)
    return bytes(output)


def _length_delimited(field_number: int, value: bytes) -> bytes:
    return _varint(field_number << 3 | 2) + _varint(len(value)) + value


def test_qqnt_protobuf_text_is_decoded_into_existing_message_elements():
    content = _varint(45002 << 3) + _varint(1) + _length_delimited(45101, "QQNT text".encode())
    raw_protobuf = _length_delimited(40800, content)

    parsed = parse_source_message(_row(raw_protobuf), _context())

    assert parsed.item["message"]["raw_message"] == "QQNT text"
    assert parsed.failures == ()


def test_qqnt_protobuf_special_types_are_decoded_without_parser_failures():
    face = _varint(45002 << 3) + _varint(6) + _length_delimited(47602, bytes.fromhex("5be6838ae590935d"))
    card = _varint(45002 << 3) + _varint(10) + _length_delimited(47901, b'{"prompt":"hello"}')
    xml = _varint(45002 << 3) + _varint(16) + _length_delimited(48602, b'<msg serviceID="35"/>')
    media = (
        _varint(45002 << 3)
        + _varint(2)
        + _length_delimited(45402, b"photo.jpg")
        + _varint(45405 << 3)
        + _varint(1234)
    )
    raw_protobuf = b"".join(
        _length_delimited(40800, value) for value in (face, card, xml, media)
    )

    parsed = parse_source_message(_row(raw_protobuf), _context())

    assert parsed.failures == ()
    assert "[CQ:face" in parsed.item["message"]["raw_message"]
    assert "[CQ:json" in parsed.item["message"]["raw_message"]
    assert "[CQ:xml" in parsed.item["message"]["raw_message"]
    assert "[CQ:image,file=photo.jpg]" in parsed.item["message"]["raw_message"]
    assert parsed.item["media"][0]["file_name"] == "photo.jpg"


def test_unparsed_protobuf_is_preserved_and_failure_is_replayable(tmp_path):
    raw_protobuf = b"\x08\xff\x10\x80"
    parsed = parse_source_message(_row(raw_protobuf), _context())
    assert parsed.item["message"]["raw_message"].startswith("[QQNT:unparsed")
    assert parsed.item["source_record"]["raw_40800_protobuf"] == base64.b64encode(raw_protobuf).decode()
    assert parsed.failures[0].error_code == "PROTOBUF_PARSE_FAILED"

    store, queue = _queue(tmp_path)
    enqueue_parsed_message(
        parsed,
        source_id="source",
        queue=queue,
        store=store,
        staging=MediaStagingStore(tmp_path / "staging", max_bytes=1024),
    )
    enqueue_parsed_message(
        parsed,
        source_id="source",
        queue=queue,
        store=store,
        staging=MediaStagingStore(tmp_path / "staging", max_bytes=1024),
    )
    assert store.status_snapshot()["unresolved_parser_failures"] == 1

    recovered = parse_source_message(_row({"elements": [{"type": "text", "text": "recovered"}]}), _context())
    enqueue_parsed_message(
        recovered,
        source_id="source",
        queue=queue,
        store=store,
        staging=MediaStagingStore(tmp_path / "staging", max_bytes=1024),
    )
    assert store.status_snapshot()["unresolved_parser_failures"] == 0
    assert queue.counts()["messages"] == {"pending": 1}


def test_staging_full_does_not_create_fake_asset_or_invalid_complete_reference(tmp_path):
    video_path = tmp_path / "large.mp4"
    video_path.write_bytes(b"x" * 128)
    parsed = parse_source_message(
        _row({"elements": [{"type": "video", "file_name": "large.mp4", "local_path": str(video_path)}]}),
        _context(),
    )
    store, queue = _queue(tmp_path)

    enqueue_parsed_message(
        parsed,
        source_id="source",
        queue=queue,
        store=store,
        staging=MediaStagingStore(tmp_path / "staging", max_bytes=64),
    )

    assert queue.counts()["media"] == {}
    message = queue.lease_ready("message", limit=1)[0]
    reference = message.payload["media"][0]
    assert (reference["source_state"], reference["archive_state"]) == ("downloaded", "failed")
    assert "asset_file_hash" not in reference
    assert store.status_snapshot()["unresolved_parser_failures"] == 1


def test_qqnt_reply_and_at_segments_use_onebot_compatible_shape():
    parsed = parse_source_message(
        _row({
            "elements": [
                {"type": "reply", "message_id": "quoted-1", "preview": "原消息"},
                {"type": "at", "user_id": "998877", "name": "Bob"},
            ]
        }),
        _context(),
    )

    assert parsed.item["message"]["message_segments"] == [
        {"type": "reply", "data": {"message_id": "quoted-1", "preview": "原消息", "id": "quoted-1"}},
        {"type": "at", "data": {"user_id": "998877", "name": "Bob", "qq": "998877"}},
    ]
    assert "[CQ:reply,id=quoted-1]" in parsed.item["message"]["raw_message"]
    assert "[CQ:at,qq=998877" in parsed.item["message"]["raw_message"]

def test_qqnt_protobuf_type_7_decodes_as_reply_with_target_and_preview():
    from collector.parsers.protobuf import _qqnt_content_element

    element = _qqnt_content_element({
        45002: [7],
        47403: [1248465005],
        47422: [7664973297536280880],
    })

    assert element == {
        "type": "reply",
        "message_id": "7664973297536280880",
        "target_user_id": "1248465005",
    }