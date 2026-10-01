"""Canonical QQNT identity and direction rules (server side).

QQNT stores private-chat direction across two numeric columns:

* ``40020`` — the sender's identity
* ``40021`` — the conversation peer's identity

``40020 != 40021`` means the account itself sent the message; ``40020 ==
40021`` means the peer did. ``40030`` carries the peer's QQ number, which is
what private conversations are keyed by.

These are **private-chat semantics only**. Group rows reuse the same column
numbers with entirely different meanings (``40020`` is the sender uid and
``40021`` is the group uid), so the two are essentially never equal there —
applying the private rule to a group row marks every message, including
everyone else's, as sent by the account. Group direction must instead come
from ``40033`` (the sender's QQ number) compared against the account id.

The collector ships as a standalone PyInstaller bundle that deliberately
excludes the ``app`` package, so it carries its own copy of these rules in
``collector/parsers/message.py``. Keep the two in step: this module is the
reference, and any change to the field semantics must be mirrored there.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

QQ_NUMBER_MIN_LENGTH = 5
QQ_NUMBER_MAX_LENGTH = 12

SENDER_IDENTITY_COLUMN = "40020"
PEER_IDENTITY_COLUMN = "40021"
PRIVATE_PEER_QQ_COLUMN = "40030"
GROUP_SENDER_QQ_COLUMN = "40033"


def is_qq_number(value: Any) -> bool:
    """True when ``value`` looks like a plain QQ number."""
    text = str(value or "").strip()
    return text.isdigit() and QQ_NUMBER_MIN_LENGTH <= len(text) <= QQ_NUMBER_MAX_LENGTH


def load_raw_columns(raw_columns_json: str | None) -> dict[str, Any]:
    """Parse a stored ``raw_columns_json`` blob, tolerating malformed values."""
    try:
        raw = json.loads(raw_columns_json or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def private_is_outgoing(raw_columns: Mapping[str, Any]) -> bool | None:
    """Direction for a *private* row, or ``None`` when undeterminable."""
    sender_identity = str(raw_columns.get(SENDER_IDENTITY_COLUMN) or "").strip()
    peer_identity = str(raw_columns.get(PEER_IDENTITY_COLUMN) or "").strip()
    if not sender_identity or not peer_identity:
        return None
    return sender_identity != peer_identity


def group_is_outgoing(raw_columns: Mapping[str, Any], account_id: str) -> bool | None:
    """Direction for a *group* row, or ``None`` when undeterminable."""
    sender_qq = str(raw_columns.get(GROUP_SENDER_QQ_COLUMN) or "").strip()
    if not sender_qq:
        return None
    return sender_qq == str(account_id)


def message_is_outgoing(
    raw_columns: Mapping[str, Any],
    *,
    message_type: str | None,
    account_id: str,
) -> bool | None:
    """Direction for any row, dispatching on ``message_type``."""
    if message_type == "private":
        return private_is_outgoing(raw_columns)
    return group_is_outgoing(raw_columns, account_id)


def private_peer_qq(raw_columns: Mapping[str, Any]) -> str | None:
    """The peer's QQ number for a private row."""
    peer = str(raw_columns.get(PRIVATE_PEER_QQ_COLUMN) or "").strip()
    return peer if is_qq_number(peer) else None
