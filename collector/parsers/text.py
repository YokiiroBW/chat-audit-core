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

def parse_at_element(element: dict[str, Any]) -> ElementResult:
    target_id = next(
        (
            element.get(key)
            for key in ("qq", "user_id", "uid", "uin", "target_id", "targetUid")
            if element.get(key) is not None
        ),
        None,
    )
    if target_id is None:
        return ElementResult("[QQNT:at,unresolved]")
    display = next(
        (element.get(key) for key in ("name", "display", "display_name", "text") if element.get(key)),
        None,
    )
    suffix = f",name={cq_escape(display)}" if display else ""
    return ElementResult(f"[CQ:at,qq={cq_escape(target_id)}{suffix}]")