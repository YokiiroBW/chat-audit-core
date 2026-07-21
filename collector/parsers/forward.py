from __future__ import annotations

import hashlib
import json
from typing import Any

from collector.parsers.base import ElementResult, GeneratedArtifact, ParserFailure
from collector.parsers.protobuf import json_safe
from collector.parsers.text import cq_escape


def _sanitize_forward(
    element: dict[str, Any],
    *,
    depth: int,
    max_depth: int,
    seen_forward_ids: set[str],
) -> tuple[dict[str, Any] | None, list[ParserFailure]]:
    forward_id = str(element.get("forward_id") or element.get("id") or "")
    if not forward_id:
        return None, [ParserFailure("FORWARD_DETAIL_NOT_CACHED", "forward element has no stable id")]
    if forward_id in seen_forward_ids:
        return None, [ParserFailure("MESSAGE_PARSE_FAILED", "forward cycle detected")]
    if depth > max_depth:
        return None, [ParserFailure("MESSAGE_PARSE_FAILED", "forward nesting depth exceeded")]
    nodes = element.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        return None, [ParserFailure("FORWARD_DETAIL_NOT_CACHED", "forward nodes are not cached locally")]
    seen = set(seen_forward_ids)
    seen.add(forward_id)
    normalized_nodes: list[dict[str, Any]] = []
    failures: list[ParserFailure] = []
    for node in nodes:
        if not isinstance(node, dict):
            failures.append(ParserFailure("MESSAGE_PARSE_FAILED", "forward node is not an object"))
            continue
        normalized = json_safe(node)
        nested = node.get("forward")
        if isinstance(nested, dict):
            nested_payload, nested_failures = _sanitize_forward(
                nested,
                depth=depth + 1,
                max_depth=max_depth,
                seen_forward_ids=seen,
            )
            failures.extend(nested_failures)
            normalized["forward"] = nested_payload or {
                "forward_id": nested.get("forward_id") or nested.get("id"),
                "unavailable": True,
            }
        normalized_nodes.append(normalized)
    return {
        "version": 1,
        "source": "qqnt_local_db",
        "forward_id": forward_id,
        "nodes": normalized_nodes,
    }, failures


def parse_forward_element(
    element: dict[str, Any],
    *,
    ordinal: int,
    max_depth: int = 3,
) -> ElementResult:
    forward_id = str(element.get("forward_id") or element.get("id") or "unknown")
    fallback = f"[CQ:forward,id={cq_escape(forward_id)}]"
    payload, failures = _sanitize_forward(
        element,
        depth=1,
        max_depth=max_depth,
        seen_forward_ids=set(),
    )
    if payload is None:
        return ElementResult(fallback, failures=failures)
    content = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    token_hash = hashlib.sha256(f"{forward_id}:{ordinal}".encode("utf-8")).hexdigest()[:16]
    token = f"__CHAT_AUDIT_FORWARD_{token_hash}__"
    artifact = GeneratedArtifact(
        ordinal=ordinal,
        content=content,
        file_name=f"forward-{token_hash}.json",
        replace_token=token,
        replacement_template=f"[CQ:forward,id={cq_escape(forward_id)},local={{local_path}}]",
        fallback_text=fallback,
    )
    return ElementResult(token, artifacts=[artifact], failures=failures)
