import json
import re
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
    containers = _candidate_containers(data)
    for key in keys:
        for container in containers:
            value = container.get(key)
            if value is not None and value != "":
                return value
    return None


def _pick_nested(data: dict[str, Any], *keys: str) -> Any:
    containers = _candidate_containers(data)
    for key in keys:
        for container in containers:
            value = container.get(key)
            if value is not None and value != "":
                return value
    return None


def _candidate_containers(data: dict[str, Any]) -> list[dict[str, Any]]:
    containers = [data]
    for key in ("data", "payload", "msg", "message"):
        value = data.get(key)
        if isinstance(value, dict):
            containers.append(value)
    return containers


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


def _normalize_msg_type(value: Any) -> str:
    numeric_map = {
        "1": "text",
        "3": "image",
        "34": "voice",
        "43": "video",
        "47": "emoji",
        "49": "share",
    }
    normalized = str(value or "text").strip().lower()
    return numeric_map.get(normalized, normalized)


def _strip_group_sender_prefix(content: str, room_id: str) -> tuple[str | None, str]:
    if not str(room_id).endswith("@chatroom"):
        return None, content
    sender, separator, body = content.partition(":\n")
    if not separator:
        return None, content
    if re.fullmatch(r"[\w.@-]{3,128}", sender):
        return sender, body
    return None, content


def _wechat_message_type(event: dict[str, Any], room_id: str) -> str:
    explicit = _pick(event, "message_type", "chat_type", "room_type", "ChatType")
    if explicit in {"group", "private"}:
        return str(explicit)
    return "group" if str(room_id).endswith("@chatroom") else "private"


def _wechat_content(event: dict[str, Any], room_id: str | None = None) -> tuple[str | None, str | None]:
    msg_type = _normalize_msg_type(_pick(event, "msg_type", "message_kind", "type", "MsgType", "msgType"))
    url = _pick_nested(event, "media_url", "file_url", "url", "Url", "URL", "cdn_url", "cdnUrl", "download_url", "FileUrl", "ThumbUrl")
    file_name = _pick_nested(event, "file_name", "filename", "name", "file", "FileName", "fileName")

    if msg_type in {"image", "img", "picture"} and url:
        return _cq_segment("image", {"file": file_name or _guess_file_name(url, "wechat-image.jpg"), "url": url}), None
    if msg_type in {"voice", "record", "audio"} and url:
        return _cq_segment("record", {"file": file_name or _guess_file_name(url, "wechat-voice.silk"), "url": url}), None
    if msg_type == "video" and url:
        return _cq_segment("video", {"file": file_name or _guess_file_name(url, "wechat-video.mp4"), "url": url}), None
    if msg_type in {"file", "attachment"} and url:
        return _cq_segment("file", {"file": file_name or _guess_file_name(url, "wechat-file.bin"), "url": url}), None
    if msg_type in {"emoji", "sticker", "emoticon"} and url:
        return _cq_segment("image", {"summary": "[动画表情]", "file": file_name or _guess_file_name(url, "wechat-emoji.gif"), "url": url}), None
    if msg_type in {"card", "json", "link", "share", "app"}:
        payload = _pick(event, "card", "payload", "data", "Content", "Xml") or event
        return _cq_segment("json", {"data": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))}), None

    raw = _pick(event, "raw_message", "raw", "content", "Content", "message_text", "text", "Text")
    if raw is not None:
        content = str(raw)
        prefixed_sender, content = _strip_group_sender_prefix(content, str(room_id or ""))
        return content, prefixed_sender

    return None, None


def normalize_wechat_event(event: dict[str, Any]) -> NormalizedWechatMessageEvent | None:
    event_type = _pick(event, "event", "post_type", "event_type", "Event", "type_name")
    if event_type is not None and str(event_type).lower() not in {"message", "message_sent", "wechat_message", "recv_msg", "new_message"}:
        return None

    robot_id = _pick(event, "robot_id", "self_id", "wxid", "account_id", "current_wxid", "CurrentWxid", "currentWxid")
    room_id = _pick(event, "room_id", "talker", "conversation_id", "from_wxid", "to_wxid", "FromUserName", "ToUserName", "fromUser", "toUser")
    sender_id = _pick(event, "sender_id", "sender", "sender_wxid", "from_user", "from_wxid", "user_id", "SenderWxid", "FromUserName", "fromUser")
    raw_message, prefixed_sender_id = _wechat_content(event, str(room_id) if room_id is not None else None)
    if robot_id is None or room_id is None or raw_message is None:
        return None

    if event.get("from_self") is True or event.get("is_self") is True:
        sender_id = robot_id
    if prefixed_sender_id is not None:
        sender_id = prefixed_sender_id
    if sender_id is None:
        sender_id = room_id

    timestamp = _pick(event, "timestamp", "time", "create_time", "createTime", "CreateTime")
    if timestamp is None:
        timestamp = int(time.time())

    nickname = _pick(event, "nickname", "sender_name", "sender_nickname", "display_name", "remark", "SenderName", "PushContent")
    message_id = _pick(event, "message_id", "msg_id", "msgid", "id", "client_msg_id", "MsgId", "NewMsgId")

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
