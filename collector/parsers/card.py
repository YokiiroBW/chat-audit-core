from __future__ import annotations

import json
from typing import Any

from collector.parsers.base import ElementResult, ParserFailure
from collector.parsers.text import cq_escape


def parse_card_element(element: dict[str, Any], *, card_type: str) -> ElementResult:
    raw_value = element.get("data") or element.get("raw") or element.get("content")
    if card_type == "json":
        if isinstance(raw_value, (dict, list)):
            raw = json.dumps(raw_value, ensure_ascii=False, separators=(",", ":"))
        elif isinstance(raw_value, str):
            raw = raw_value
            try:
                json.loads(raw)
            except json.JSONDecodeError:
                return ElementResult(
                    f"[QQNT:card,type=json]",
                    failures=[ParserFailure("CARD_PARSE_FAILED", "JSON card payload is invalid")],
                )
        else:
            return ElementResult(
                "[QQNT:card,type=json]",
                failures=[ParserFailure("CARD_PARSE_FAILED", "JSON card payload is missing")],
            )
    else:
        if not isinstance(raw_value, str) or not raw_value.strip():
            return ElementResult(
                "[QQNT:card,type=xml]",
                failures=[ParserFailure("CARD_PARSE_FAILED", "XML card payload is missing")],
            )
        raw = raw_value
    return ElementResult(f"[CQ:{card_type},data={cq_escape(raw)}]")
