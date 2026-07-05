import json

from wechat_tray_adapter.client import NasClientError, encode_multipart_form
from wechat_tray_adapter.config import AdapterConfig
from wechat_tray_adapter.mapper import build_nas_event, discover_media_path, media_upload_type
from wechat_tray_adapter.queue import PendingEventQueue
from wechat_tray_adapter.worker import SyncWorker


class FakeClient:
    def __init__(self, fail_send: bool = False):
        self.fail_send = fail_send
        self.uploads = []
        self.events = []

    def upload_media(self, path: str, media_type: str, file_name: str | None = None):
        self.uploads.append((path, media_type, file_name))
        return {
            "local_path": "/static/storage/0123456789abcdef0123456789abcdef.jpg",
            "file_name": file_name,
            "media_type": media_type,
        }

    def send_event(self, payload):
        if self.fail_send:
            raise NasClientError("offline")
        self.events.append(payload)
        return {"msg_hash": "ok", "skipped": False}


def test_adapter_config_loads_file_and_env_overrides(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"nas_url": "http://nas.local:8000", "account_id": "wxid_file"}), encoding="utf-8")

    config = AdapterConfig.load(
        config_path,
        env={
            "APPDATA": str(tmp_path),
            "CHAT_AUDIT_TOKEN": "env-token",
            "CHAT_AUDIT_WECHAT_ACCOUNT_ID": "wxid_env",
        },
    )

    assert config.normalized_nas_url == "http://nas.local:8000"
    assert config.token == "env-token"
    assert config.account_id == "wxid_env"
    assert config.queue_db == tmp_path / "ChatAuditWechatTray" / "queue.sqlite3"


def test_encode_multipart_form_contains_file_and_fields():
    body, content_type = encode_multipart_form(
        {"media_type": "image"},
        {"file": ("image.jpg", b"abc", "image/jpeg")},
    )

    assert content_type.startswith("multipart/form-data; boundary=")
    assert b'name="media_type"' in body
    assert b"image" in body
    assert b'filename="image.jpg"' in body
    assert b"abc" in body


def test_mapper_builds_wechat_ferry_event_with_uploaded_media(tmp_path):
    image_path = tmp_path / "wechat.jpg"
    image_path.write_bytes(b"image")
    raw = {
        "self_wxid": "wxid_raw",
        "roomid": "room@chatroom",
        "sender": "wxid_sender",
        "type": 3,
        "content": "image xml",
        "thumb": str(image_path),
        "msgid": "msg-1",
        "time": 1783100500,
    }

    assert discover_media_path(raw) == image_path
    assert media_upload_type(raw) == "image"
    event = build_nas_event(
        raw,
        AdapterConfig(account_id="wxid_config", account_name="WeChat Bot"),
        uploaded_media={"local_path": "/static/storage/a.jpg", "file_name": "wechat.jpg"},
    )

    assert event["robot_id"] == "wxid_config"
    assert event["roomid"] == "room@chatroom"
    assert event["sender"] == "wxid_sender"
    assert event["type"] == "image"
    assert event["uploaded_path"] == "/static/storage/a.jpg"
    assert event["file_name"] == "wechat.jpg"


def test_pending_queue_persists_failures(tmp_path):
    queue = PendingEventQueue(tmp_path / "queue.sqlite3")
    item_id = queue.enqueue("message", {"hello": "wechat"})

    items = queue.list_pending()
    assert [item.id for item in items] == [item_id]
    assert items[0].payload == {"hello": "wechat"}

    queue.mark_failed(item_id, "offline")
    failed = queue.list_pending()[0]
    assert failed.attempts == 1
    assert failed.last_error == "offline"

    queue.mark_done(item_id)
    assert queue.list_pending() == []


def test_sync_worker_uploads_media_and_queues_when_nas_is_offline(tmp_path):
    image_path = tmp_path / "wechat.jpg"
    image_path.write_bytes(b"image")
    queue = PendingEventQueue(tmp_path / "queue.sqlite3")
    config = AdapterConfig(account_id="wxid_bot", queue_db=tmp_path / "queue.sqlite3")
    client = FakeClient(fail_send=True)
    worker = SyncWorker(config, client, queue)

    result = worker.handle_wcf_message(
        {
            "roomid": "room@chatroom",
            "sender": "wxid_sender",
            "type": 3,
            "thumb": str(image_path),
            "msgid": "msg-1",
        }
    )

    assert result is None
    assert client.uploads == [(str(image_path), "image", "wechat.jpg")]
    assert len(queue.list_pending()) == 1


def test_sync_worker_flushes_pending_message(tmp_path):
    queue = PendingEventQueue(tmp_path / "queue.sqlite3")
    queue.enqueue(
        "message",
        {
            "payload": {
                "event": "message",
                "robot_id": "wxid_bot",
                "roomid": "wxid_friend",
                "sender": "wxid_friend",
                "type": "text",
                "content": "hello",
                "time": 1783100600,
                "msgid": "msg-2",
            }
        },
    )
    client = FakeClient()
    worker = SyncWorker(AdapterConfig(account_id="wxid_bot", queue_db=tmp_path / "queue.sqlite3"), client, queue)

    assert worker.flush_pending() == 1
    assert queue.list_pending() == []
    assert client.events[0]["content"] == "hello"
