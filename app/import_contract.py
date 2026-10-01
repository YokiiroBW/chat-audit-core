from __future__ import annotations

import hashlib
import json
from typing import Any


IMPORT_SOURCE_TYPE_QQNT = "qqnt_local_db"
IMPORT_SOURCE_STATUSES = frozenset({"active", "needs_key", "disabled", "error"})
IMPORT_BATCH_MODES = frozenset({"initial", "incremental", "reconcile", "media_rescan"})
IMPORT_BATCH_STATUSES = frozenset({"running", "completed", "partial", "failed", "cancelled"})
SQL_INTEGER_MIN = -(1 << 31)
SQL_INTEGER_MAX = (1 << 31) - 1

MEDIA_SOURCE_STATES = frozenset({"downloaded", "not_downloaded", "missing", "unknown"})
MEDIA_ARCHIVE_STATES = frozenset({"complete", "thumbnail_only", "metadata_only", "failed"})

VALID_MEDIA_STATE_COMBINATIONS = frozenset(
    {
        ("downloaded", "complete"),
        ("downloaded", "failed"),
        ("not_downloaded", "thumbnail_only"),
        ("not_downloaded", "metadata_only"),
        ("missing", "thumbnail_only"),
        ("missing", "metadata_only"),
        ("unknown", "failed"),
    }
)

# Explicit transitions keep import replays monotonic. A complete NAS archive is
# terminal even if a later source-device scan no longer sees the QQ cache file.
ALLOWED_MEDIA_STATE_TRANSITIONS = {
    ("unknown", "failed"): VALID_MEDIA_STATE_COMBINATIONS,
    ("not_downloaded", "metadata_only"): frozenset(
        {
            ("not_downloaded", "metadata_only"),
            ("not_downloaded", "thumbnail_only"),
            ("downloaded", "failed"),
            ("downloaded", "complete"),
        }
    ),
    ("not_downloaded", "thumbnail_only"): frozenset(
        {
            ("not_downloaded", "thumbnail_only"),
            ("downloaded", "failed"),
            ("downloaded", "complete"),
        }
    ),
    ("missing", "metadata_only"): frozenset(
        {
            ("missing", "metadata_only"),
            ("missing", "thumbnail_only"),
            ("downloaded", "failed"),
            ("downloaded", "complete"),
        }
    ),
    ("missing", "thumbnail_only"): frozenset(
        {
            ("missing", "thumbnail_only"),
            ("downloaded", "failed"),
            ("downloaded", "complete"),
        }
    ),
    ("downloaded", "failed"): frozenset(
        {
            ("downloaded", "failed"),
            ("downloaded", "complete"),
        }
    ),
    ("downloaded", "complete"): frozenset({("downloaded", "complete")}),
}


def qqnt_source_identity(
    *,
    account_id: str,
    chat_type: str,
    conversation_id: str,
    msg_id: str | int,
    msg_random: str | int,
    msg_seq: str | int,
) -> str:
    return (
        f"qqnt:{account_id}:{chat_type}:{conversation_id}:"
        f"{msg_id}:{msg_random}:{msg_seq}"
    )


def qqnt_external_message_id(**identity: Any) -> str:
    source_identity = qqnt_source_identity(**identity)
    return hashlib.sha256(source_identity.encode("utf-8")).hexdigest()


def stable_import_source_id(*, source_type: str, account_id: str, device_id: str) -> str:
    identity = f"{source_type}:{account_id}:{device_id}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def canonical_payload_hash(payload: Any) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_media_state_combination(source_state: str, archive_state: str) -> None:
    state = (source_state, archive_state)
    if state not in VALID_MEDIA_STATE_COMBINATIONS:
        raise ValueError(f"invalid media state combination: {source_state} + {archive_state}")


def can_transition_media_state(
    current_source_state: str,
    current_archive_state: str,
    incoming_source_state: str,
    incoming_archive_state: str,
) -> bool:
    current = (current_source_state, current_archive_state)
    incoming = (incoming_source_state, incoming_archive_state)
    validate_media_state_combination(*current)
    validate_media_state_combination(*incoming)
    return incoming in ALLOWED_MEDIA_STATE_TRANSITIONS[current]


def validate_media_reference_assets(
    *,
    source_state: str,
    archive_state: str,
    asset_file_hash: str | None,
    thumbnail_file_hash: str | None,
) -> None:
    validate_media_state_combination(source_state, archive_state)
    if archive_state == "complete" and not asset_file_hash:
        raise ValueError("archive_state=complete requires asset_file_hash")
    if archive_state == "thumbnail_only" and not thumbnail_file_hash:
        raise ValueError("archive_state=thumbnail_only requires thumbnail_file_hash")
    if source_state == "not_downloaded" and asset_file_hash:
        raise ValueError("source_state=not_downloaded cannot reference a complete media asset")
