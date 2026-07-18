from __future__ import annotations

from typing import Any

from collector.parsers.base import ElementResult


def cq_escape(value: Any) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("[", "&#91;")
        .replace("]", "&#93;")
        .replace(",", "&#44;")
    )


def parse_text_element(element: dict[str, Any]) -> ElementResult:
    return ElementResult(str(element.get("text") or element.get("content") or ""))


def parse_face_element(element: dict[str, Any]) -> ElementResult:
    face_id = cq_escape(element.get("id") or element.get("face_id") or "unknown")
    description = element.get("description") or element.get("name")
    suffix = f",name={cq_escape(description)}" if description else ""
    return ElementResult(f"[CQ:face,id={face_id}{suffix}]")
