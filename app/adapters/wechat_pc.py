import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class NormalizedWechatMessageEvent:
    robot_id: str
    platform: str
    msg_data: dict[str, Any]


def _pick(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = data.get(key)
        if value is not None and value != "":
            return value
    return None


def _pick_nested(data: dict[str, Any], *keys: str) -> Any:
    nested = data.get("data") if isinstance(data.get("data"), dict) else {}
    for key in keys:
        value = data.get(key)
        if value is not None and value != "":
            return value
        value = nested.get(key)
        if value is not None and value != "":
            return value
    return None


def _cq_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace(",", "&#44;")
        .replace("[", "&#91;")
        .replace("]", "&#93;")
    )


def _cq_segment(kind: str, params: dict[str, Any]) -> str:
    return f"[CQ:{kind}," + ",".join(f"{key}={_cq_escape(str(value))}" for key, value in params.items() if value is not None) + "]"


def _guess_file_name(url: str | None, fallback: str) -> str:
    if not url:
        return fallback
    name = Path(str(url).split("?", 1)[0]).name
    return name or fallback


def _wechat_message_type(event: dict[str, Any], room_id: str) -> str:
    explicit = _pick(event, "message_type", "chat_type", "room_type")
    if explicit in {"group", "private"}:
        return str(explicit)
    return "group" if str(room_id).endswith("@chatroom") else "private"


def _wechat_content(event: dict[str, Any]) -> str | None:
    raw = _pick(event, "raw_message", "raw", "content", "message", "text")
    if raw is not None:
        return str(raw)

    msg_type = str(_pick(event, "msg_type", "message_kind", "type") or "text").lower()
    url = _pick_nested(event, "media_url", "file_url", "url", "cdn_url", "download_url")
    file_name = _pick_nested(event, "file_name", "filename", "name", "file")

    if msg_type in {"image", "img", "picture"} and url:
        return _cq_segment("image", {"file": file_name or _guess_file_name(url, "wechat-image.jpg"), "url": url})
    if msg_type in {"voice", "record", "audio"} and url:
        return _cq_segment("record", {"file": file_name or _guess_file_name(url, "wechat-voice.silk"), "url": url})
    if msg_type == "video" and url:
        return _cq_segment("video", {"file": file_name or _guess_file_name(url, "wechat-video.mp4"), "url": url})
    if msg_type in {"file", "attachment"} and url:
        return _cq_segment("file", {"file": file_name or _guess_file_name(url, "wechat-file.bin"), "url": url})
    if msg_type in {"card", "json", "link", "share"}:
        payload = _pick(event, "card", "payload", "data") or event
        return _cq_segment("json", {"data": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))})

    return None


def normalize_wechat_event(event: dict[str, Any]) -> NormalizedWechatMessageEvent | None:
    event_type = _pick(event, "event", "post_type", "event_type")
    if event_type is not None and str(event_type).lower() not in {"message", "message_sent", "wechat_message"}:
        return None

    robot_id = _pick(event, "robot_id", "self_id", "wxid", "account_id", "current_wxid")
    room_id = _pick(event, "room_id", "talker", "conversation_id", "from_wxid", "to_wxid")
    sender_id = _pick(event, "sender_id", "sender", "sender_wxid", "from_user", "from_wxid", "user_id")
    raw_message = _wechat_content(event)
    if robot_id is None or room_id is None or raw_message is None:
        return None

    if event.get("from_self") is True or event.get("is_self") is True:
        sender_id = robot_id
    if sender_id is None:
        sender_id = room_id

    timestamp = _pick(event, "timestamp", "time", "create_time", "createTime")
    if timestamp is None:
        timestamp = int(time.time())

    nickname = _pick(event, "nickname", "sender_name", "sender_nickname", "display_name", "remark")
    message_id = _pick(event, "message_id", "msg_id", "msgid", "id", "client_msg_id")

    msg_data = {
        "room_id": str(room_id),
        "message_type": _wechat_message_type(event, str(room_id)),
        "sender_id": str(sender_id),
        "nickname": str(nickname) if nickname is not None else None,
        "raw_message": raw_message,
        "local_message": raw_message,
        "timestamp": int(timestamp),
    }
    if message_id is not None:
        msg_data["message_id"] = str(message_id)

    return NormalizedWechatMessageEvent(robot_id=str(robot_id), platform="wechat", msg_data=msg_data)
