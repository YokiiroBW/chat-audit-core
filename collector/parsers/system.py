from __future__ import annotations

from typing import Any

from collector.parsers.base import ElementResult
from collector.parsers.text import cq_escape


def parse_system_element(element: dict[str, Any]) -> ElementResult:
    event_type = cq_escape(element.get("event_type") or element.get("type") or "system")
    text = str(element.get("text") or element.get("content") or "")
    return ElementResult(f"[QQNT:system,type={event_type}]{text}")
