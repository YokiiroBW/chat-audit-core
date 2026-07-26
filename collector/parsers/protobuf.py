from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DecodedMessagePayload:
    document: Any | None
    raw_base64: str | None
    error_code: str | None = None



def _read_varint(raw: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while offset < len(raw) and shift <= 63:
        byte = raw[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
    raise ValueError("invalid protobuf varint")


def _protobuf_fields(raw: bytes) -> dict[int, list[Any]]:
    fields: dict[int, list[Any]] = {}
    offset = 0
    while offset < len(raw):
        tag, offset = _read_varint(raw, offset)
        field_number = tag >> 3
        wire_type = tag & 0x07
        if field_number <= 0:
            raise ValueError("invalid protobuf field number")
        if wire_type == 0:
            value, offset = _read_varint(raw, offset)
        elif wire_type == 1:
            value = raw[offset : offset + 8]
            offset += 8
        elif wire_type == 2:
            length, offset = _read_varint(raw, offset)
            value = raw[offset : offset + length]
            offset += length
        elif wire_type == 5:
            value = raw[offset : offset + 4]
            offset += 4
        else:
            raise ValueError("unsupported protobuf wire type")
        if offset > len(raw):
            raise ValueError("truncated protobuf field")
        fields.setdefault(field_number, []).append(value)
    return fields


def _decode_text(value: bytes) -> str | None:
    try:
        text = value.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None
    if not text or "\ufffd" in text:
        return None
    if any(ord(char) < 32 and char not in "\t\r\n" for char in text):
        return None
    return text


def _text_values(fields: dict[int, list[Any]], field_number: int) -> list[str]:
    values: list[str] = []
    for value in fields.get(field_number, []):
        if not isinstance(value, bytes):
            continue
        text = _decode_text(value)
        if text:
            values.append(text)
    return values


def _first_text(fields: dict[int, list[Any]], *field_numbers: int) -> str:
    for field_number in field_numbers:
        values = _text_values(fields, field_number)
        if values:
            return values[-1]
    return ""


def _first_integer(fields: dict[int, list[Any]], field_number: int, default: int = 0) -> int:
    values = fields.get(field_number, [])
    if not values:
        return default
    value = values[-1]
    return int(value) if isinstance(value, int) else default


def _looks_like_json(value: str) -> bool:
    stripped = value.strip()
    return stripped.startswith(("{", "["))


def _looks_like_xml(value: str) -> bool:
    return value.lstrip().startswith(("<?xml", "<msg", "<gtip"))


def _human_text(value: str) -> bool:
    if not value or len(value) > 8192:
        return False
    if value.startswith(("u_", "http://", "https://", "/download?", "::NTOSFull::")):
        return False
    if "\ufffd" in value or any(ord(char) < 32 and char not in "\t\r\n" for char in value):
        return False
    return any(char.isspace() or ord(char) > 127 for char in value)


def _generic_text(fields: dict[int, list[Any]]) -> str:
    candidates: list[str] = []
    for values in fields.values():
        for value in values:
            if not isinstance(value, bytes) or not value:
                continue
            text = _decode_text(value)
            if text and _human_text(text) and text not in candidates:
                candidates.append(text)
    return " ".join(candidates[:4])


def _media_element(fields: dict[int, list[Any]], content_type: int) -> dict[str, Any] | None:
    file_name = _first_text(fields, 45402)
    if not file_name:
        return None
    local_path = _first_text(fields, 45403)
    if local_path.startswith("::NTOSFull::"):
        local_path = local_path[len("::NTOSFull::") :]
    thumbnail_path = _first_text(fields, 45422)
    suffix = file_name.lower()
    if content_type == 2:
        element_type = "image"
    elif content_type == 5 or suffix.endswith((".mp4", ".mov", ".mkv", ".avi")):
        element_type = "video"
    else:
        element_type = "file"
    element: dict[str, Any] = {
        "type": element_type,
        "file_name": file_name,
        "local_path": local_path,
        "file_size": _first_integer(fields, 45405),
    }
    if thumbnail_path:
        element["thumbnail_path"] = thumbnail_path
        element["thumbnail_name"] = thumbnail_path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
    return element


# A reply quotes its target, and the quoted content can itself be a reply.
# Nothing in the wire format stops that nesting, so a malformed or crafted row
# would recurse until Python gave up and the RecursionError aborted the scan of
# the whole database, not just that one message.
MAX_CONTENT_DEPTH = 3


def _reply_preview(fields: dict[int, list[Any]], depth: int, max_depth: int) -> str:
    for value in fields.get(47423, []):
        if not isinstance(value, bytes):
            continue
        try:
            nested = _protobuf_fields(value)
        except ValueError:
            continue
        element = _qqnt_content_element(nested, depth + 1, max_depth)
        element_type = element.get("type")
        if element_type == "text":
            return str(element.get("text") or "")
        if element_type == "image":
            return "[图片]"
        if element_type == "video":
            return "[视频]"
        if element_type == "record":
            return "[语音]"
        if element_type == "file":
            return "[文件]"
        if element_type == "face":
            return str(element.get("description") or "[表情]")
    return ""

def _qqnt_content_element(fields: dict[int, list[Any]], depth: int = 0, max_depth: int = MAX_CONTENT_DEPTH) -> dict[str, Any]:
    content_type = _first_integer(fields, 45002)
    if content_type == 7:
        reply_id = _first_integer(fields, 47422)
        element = {"type": "reply", "message_id": str(reply_id) if reply_id else ""}
        target_user_id = _first_integer(fields, 47403)
        if target_user_id:
            element["target_user_id"] = str(target_user_id)
        # Past the limit the reply is still returned, just without the quoted
        # preview: losing one preview beats losing the rest of the database.
        preview = _reply_preview(fields, depth, max_depth) if depth < max_depth else ""
        if preview:
            element["preview"] = preview
        return element
    text_values = _text_values(fields, 45101)
    if text_values:
        return {"type": "text", "text": "".join(text_values)}

    if content_type in {2, 3, 4, 5}:
        media = _media_element(fields, content_type)
        if media is not None:
            return media

    face_text = _first_text(fields, 47602, 80900)
    if content_type == 6 and face_text:
        return {"type": "face", "id": _first_integer(fields, 47604, 0), "description": face_text}

    json_text = _first_text(fields, 47901, 48271)
    if json_text:
        try:
            return {"type": "json", "data": json.loads(json_text)}
        except json.JSONDecodeError:
            if _looks_like_json(json_text):
                return {"type": "text", "text": json_text}

    xml_text = _first_text(fields, 48602, 48214)
    if xml_text and _looks_like_xml(xml_text):
        return {"type": "xml", "data": xml_text}

    forward_text = _first_text(fields, 48701)
    if forward_text:
        return {"type": "text", "text": forward_text}

    generic_text = _generic_text(fields)
    if generic_text:
        return {"type": "text", "text": generic_text}
    return {
        "type": "system",
        "event_type": f"qqnt_type_{content_type}",
        "text": f"[QQNT:type={content_type}]",
    }


def _qqnt_protobuf_document(raw: bytes) -> Any | None:
    try:
        outer = _protobuf_fields(raw)
    except ValueError:
        return None
    contents = outer.get(40800, [])
    if not contents or not all(isinstance(value, bytes) for value in contents):
        return None
    elements: list[dict[str, Any]] = []
    for content_raw in contents:
        try:
            content = _protobuf_fields(content_raw)
        except ValueError:
            return None
        elements.append(_qqnt_content_element(content))
    return {"elements": elements} if elements else None

def decode_message_payload(value: Any) -> DecodedMessagePayload:
    if isinstance(value, (dict, list)):
        return DecodedMessagePayload(value, None)
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith(("{", "[")):
            try:
                return DecodedMessagePayload(json.loads(stripped), None)
            except json.JSONDecodeError:
                pass
        return DecodedMessagePayload({"elements": [{"type": "text", "text": value}]}, None)
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        raw_base64 = base64.b64encode(raw).decode("ascii")
        qqnt_document = _qqnt_protobuf_document(raw)
        if qqnt_document is not None:
            return DecodedMessagePayload(qqnt_document, raw_base64)
        try:
            decoded = raw.decode("utf-8")
        except UnicodeDecodeError:
            return DecodedMessagePayload(None, raw_base64, "PROTOBUF_PARSE_FAILED")
        try:
            return DecodedMessagePayload(json.loads(decoded), raw_base64)
        except json.JSONDecodeError:
            return DecodedMessagePayload(None, raw_base64, "PROTOBUF_PARSE_FAILED")
    return DecodedMessagePayload(None, None, "MESSAGE_PARSE_FAILED")


def json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): json_safe(nested) for key, nested in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"encoding": "base64", "data": base64.b64encode(bytes(value)).decode("ascii")}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)
