from __future__ import annotations

from pathlib import Path
import time
from typing import Any

from wechat_tray_adapter.config import AdapterConfig


WCF_TYPE_TO_KIND = {
    1: "text",
    3: "image",
    34: "voice",
    43: "video",
    47: "emoji",
    49: "share",
}

MEDIA_KIND_TO_UPLOAD_TYPE = {
    "image": "image",
    "emoji": "image",
    "voice": "voice",
    "video": "video",
    "file": "file",
}


def normalize_wcf_type(value: Any) -> str:
    try:
        numeric = int(value)
    except (TypeError, ValueError):
        return str(value or "text").strip().lower()
    return WCF_TYPE_TO_KIND.get(numeric, str(numeric))


def pick(raw: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = raw.get(key)
        if value not in (None, ""):
            return value
    return None


def discover_media_path(raw: dict[str, Any]) -> Path | None:
    for key in ("local_path", "path", "file_path", "thumb", "extra"):
        value = raw.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        if value.startswith(("/static/storage/", "http://", "https://")):
            continue
        candidate = Path(value)
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def media_upload_type(raw: dict[str, Any]) -> str | None:
    kind = normalize_wcf_type(pick(raw, "msg_type", "type", "MsgType"))
    return MEDIA_KIND_TO_UPLOAD_TYPE.get(kind)


def build_nas_event(raw: dict[str, Any], config: AdapterConfig, uploaded_media: dict[str, Any] | None = None) -> dict[str, Any]:
    kind = normalize_wcf_type(pick(raw, "msg_type", "type", "MsgType"))
    room_id = pick(raw, "roomid", "room_id", "talker", "from_wxid", "FromUserName")
    sender_id = pick(raw, "sender", "sender_wxid", "sender_id", "from_wxid", "SenderWxid")
    robot_id = config.account_id or pick(raw, "self_wxid", "wxid", "robot_id", "current_wxid", "CurrentWxid")
    content = pick(raw, "content", "Content", "text", "message")

    event = {
        "event": "message",
        "robot_id": str(robot_id or ""),
        "roomid": str(room_id or sender_id or ""),
        "sender": str(sender_id or room_id or ""),
        "type": kind,
        "content": str(content or ""),
        "time": int(pick(raw, "time", "timestamp", "ts", "create_time", "CreateTime") or time.time()),
        "msgid": str(pick(raw, "msg_id", "msgid", "id", "MsgId") or ""),
        "account_name": config.account_name,
    }
    if uploaded_media:
        event["uploaded_path"] = uploaded_media.get("local_path")
        event["file_name"] = uploaded_media.get("file_name")
    return event
