from __future__ import annotations

from typing import Any

from collector.parsers.base import ElementResult, ParserFailure
from collector.parsers.text import cq_escape


def parse_reply_element(element: dict[str, Any]) -> ElementResult:
    message_id = element.get("message_id") or element.get("msg_id") or element.get("source_message_id")
    if message_id is None:
        return ElementResult(
            "[QQNT:reply,unresolved]",
            failures=[ParserFailure("MESSAGE_PARSE_FAILED", "reply element has no source message id")],
        )
    preview = element.get("preview") or element.get("text_preview")
    suffix = str(preview) if preview else ""
    return ElementResult(f"[CQ:reply,id={cq_escape(message_id)}]{suffix}")
