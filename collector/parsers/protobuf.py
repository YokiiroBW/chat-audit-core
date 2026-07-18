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
