"""Explicit, content-pinned historical backup verification.

The registry is trusted configuration supplied by the operator. Backup input
cannot register itself, and historical keys are never used to sign output.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping


BACKUP_TRUST_SCHEMA = "chat-audit-core.backup-trust.v1"
BACKUP_CONTENT_DOMAIN = "chat-audit-core.backup-content.v1"
BACKUP_SIGNATURE_ALGORITHM = "hmac-sha256"
MAX_REGISTRY_BYTES = 2 * 1024 * 1024
MAX_HISTORICAL_KEYS = 16
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
logger = logging.getLogger(__name__)


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def backup_content_fingerprint(
    format_name: str, payload_sha256: str, manifest: dict[str, Any]
) -> str:
    """Bind the full parsed manifest to the checksum calculated from the input.

    Legacy payload checksums use its existing canonical JSON replay, while v4
    uses the existing member framing. Never pass a checksum merely read from
    the manifest. Including checksum/signature fields also pins their metadata.
    """

    if format_name not in {"legacy-json", "v4"} or not _SHA256_RE.fullmatch(payload_sha256):
        raise ValueError("unsupported backup content fingerprint input")
    return hashlib.sha256(canonical_json_bytes({
        "domain": BACKUP_CONTENT_DOMAIN,
        "format": format_name,
        "payload_sha256": payload_sha256,
        "manifest": manifest,
    })).hexdigest()


@dataclass(frozen=True)
class BackupVerificationPolicy:
    historical_keys: Mapping[str, str] = field(default_factory=dict, repr=False)
    trusted_backups: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        keys = dict(self.historical_keys)
        pins = dict(self.trusted_backups)
        if len(keys) > MAX_HISTORICAL_KEYS or any(
            not isinstance(name, str) or not name or not isinstance(key, str) or not key
            for name, key in keys.items()
        ):
            raise ValueError("invalid historical backup keys")
        if any(
            not isinstance(pin, str) or not _SHA256_RE.fullmatch(pin)
            or not isinstance(name, str) or name not in keys
            for pin, name in pins.items()
        ):
            raise ValueError("invalid trusted backup fingerprints")
        object.__setattr__(self, "historical_keys", MappingProxyType(keys))
        object.__setattr__(self, "trusted_backups", MappingProxyType(pins))

    def key_for_content(
        self, format_name: str, payload_sha256: str, manifest: dict[str, Any]
    ) -> tuple[str, str] | None:
        pin = backup_content_fingerprint(format_name, payload_sha256, manifest)
        key_id = self.trusted_backups.get(pin)
        if key_id is None:
            return None
        return key_id, self.historical_keys[key_id]


def _unique_object(pairs) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate registry key")
        value[key] = item
    return value


def load_backup_verification_policy(registry_path: str | Path | None) -> BackupVerificationPolicy:
    """A broken optional registry disables only historical verification.

    Diagnostics deliberately omit file contents, key values and exception text.
    Current-key verification and the explicit unsigned import mode still work.
    """

    if not registry_path:
        return BackupVerificationPolicy()
    try:
        with Path(registry_path).open("rb") as source:
            content = source.read(MAX_REGISTRY_BYTES + 1)
        if len(content) > MAX_REGISTRY_BYTES:
            raise ValueError("backup trust registry exceeds its bounded size")
        value = json.loads(content, object_pairs_hook=_unique_object)
        if (
            not isinstance(value, dict) or value.get("schema") != BACKUP_TRUST_SCHEMA
            or not isinstance(value.get("historical_keys"), dict)
            or not isinstance(value.get("trusted_backups"), dict)
        ):
            raise ValueError("unsupported backup trust registry")
        return BackupVerificationPolicy(value["historical_keys"], value["trusted_backups"])
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        logger.warning(
            "Backup trust registry unavailable or invalid; historical verification disabled (%s)",
            type(exc).__name__,
        )
        return BackupVerificationPolicy()
