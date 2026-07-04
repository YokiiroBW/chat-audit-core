from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class NormalizedMessageEvent:
    robot_id: str
    platform: str
    msg_data: dict[str, Any]


def _pick_nickname(sender: dict[str, Any]) -> str | None:
    card = sender.get("card")
    nickname = sender.get("nickname")
    if card:
        return str(card)
    if nickname:
        return str(nickname)
    return None


def normalize_message_event(event: dict[str, Any]) -> NormalizedMessageEvent | None:
    if event.get("post_type") not in {"message", "message_sent"}:
        return None

    message_type = event.get("message_type")
    if message_type not in {"group", "private"}:
        return None

    self_id = event.get("self_id")
    user_id = event.get("user_id")
    raw_message = event.get("raw_message")
    timestamp = event.get("time")
    if self_id is None or user_id is None or raw_message is None or timestamp is None:
        return None

    if message_type == "group":
        room_source = event.get("group_id")
    else:
        room_source = event.get("user_id")
    if room_source is None:
        return None

    sender = event.get("sender") or {}
    msg_data = {
        "room_id": str(room_source),
        "message_type": str(message_type),
        "sender_id": str(user_id),
        "nickname": _pick_nickname(sender),
        "raw_message": str(raw_message),
        "local_message": str(raw_message),
        "timestamp": int(timestamp),
    }
    if event.get("message_id") is not None:
        msg_data["message_id"] = str(event["message_id"])
    return NormalizedMessageEvent(robot_id=str(self_id), platform="qq", msg_data=msg_data)
