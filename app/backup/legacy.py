from __future__ import annotations

import base64
import copy
import gzip
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO, Iterator

import ijson

from app.backup.archive import BackupArchiveWriter, canonical_json_bytes
from app.backup.exporter import BackupExportResult, backup_filename
from app.services.backup_service import (
    BACKUP_SCHEMA,
    BACKUP_SCHEMA_V2,
    BACKUP_SIGNATURE_ALGORITHM,
    SUPPORTED_BACKUP_SCHEMAS,
    BackupService,
)
from app.time_utils import format_utc_z, utc_now


LEGACY_SECTIONS = (
    "import_sources",
    "import_batches",
    "messages",
    "robot_messages",
    "message_source_records",
    "message_media_references",
    "message_parts",
    "identity_aliases",
    "profile_change_records",
    "room_profiles",
    "user_profiles",
    "media_assets",
)
BASE64_DECODE_CHARS = 4 * 1024 * 1024


class LegacyBackupValidationError(ValueError):
    pass


@contextmanager
def _legacy_input(path: Path) -> Iterator[BinaryIO]:
    raw = Path(path).open("rb")
    try:
        signature = raw.read(2)
        raw.seek(0)
        if signature == b"\x1f\x8b":
            with gzip.GzipFile(fileobj=raw, mode="rb") as decompressed:
                yield decompressed
        else:
            yield raw
    finally:
        raw.close()


def _build_event_value(first_event: str, first_value: Any, events) -> Any:
    if first_event in {"string", "number", "boolean", "null"}:
        return first_value
    if first_event == "start_map":
        result: dict[str, Any] = {}
        while True:
            event, value = next(events)
            if event == "end_map":
                return result
            if event != "map_key":
                raise LegacyBackupValidationError(f"expected map key, got {event}")
            value_event, value_value = next(events)
            result[str(value)] = _build_event_value(value_event, value_value, events)
    if first_event == "start_array":
        result = []
        while True:
            event, value = next(events)
            if event == "end_array":
                return result
            result.append(_build_event_value(event, value, events))
    raise LegacyBackupValidationError(f"unexpected JSON event: {first_event}")


def _spool_legacy_top_level(source: BinaryIO, workspace: Path) -> tuple[dict[str, Path], dict[str, int]]:
    events = iter(ijson.basic_parse(source, use_float=True))
    try:
        first_event, _first_value = next(events)
    except StopIteration as exc:
        raise LegacyBackupValidationError("legacy backup is empty") from exc
    if first_event != "start_map":
        raise LegacyBackupValidationError("legacy backup package must be an object")
    spools: dict[str, Path] = {}
    item_counts: dict[str, int] = {}
    seen_keys: set[str] = set()
    while True:
        try:
            event, key = next(events)
        except StopIteration as exc:
            raise LegacyBackupValidationError("legacy backup ended before its top-level object closed") from exc
        if event == "end_map":
            break
        if event != "map_key" or not isinstance(key, str):
            raise LegacyBackupValidationError(f"invalid legacy top-level event: {event}")
        if key in seen_keys:
            raise LegacyBackupValidationError(f"duplicate legacy top-level key: {key}")
        seen_keys.add(key)
        value_event, value = next(events)
        spool_path = workspace / f"legacy-{len(spools):03d}-{hashlib.sha256(key.encode()).hexdigest()[:12]}.json"
        count = 0
        with spool_path.open("xb") as output:
            if value_event == "start_array":
                output.write(b"[")
                first = True
                while True:
                    item_event, item_value = next(events)
                    if item_event == "end_array":
                        break
                    item = _build_event_value(item_event, item_value, events)
                    if not first:
                        output.write(b",")
                    output.write(canonical_json_bytes(item))
                    first = False
                    count += 1
                output.write(b"]")
            else:
                parsed = _build_event_value(value_event, value, events)
                output.write(canonical_json_bytes(parsed))
                count = 1
            output.flush()
            os.fsync(output.fileno())
        spools[key] = spool_path
        item_counts[key] = count
    try:
        next(events)
    except StopIteration:
        return spools, item_counts
    raise LegacyBackupValidationError("legacy backup has trailing JSON values")


def _load_small_spool(path: Path, *, label: str, max_bytes: int = 2 * 1024 * 1024) -> Any:
    if path.stat().st_size > max_bytes:
        raise LegacyBackupValidationError(f"legacy {label} exceeds its bounded size")
    return json.loads(path.read_bytes())


def _legacy_manifest_for_digest(manifest: dict[str, Any]) -> dict[str, Any]:
    value = copy.deepcopy(manifest)
    value.pop("checksum", None)
    value.pop("signature", None)
    return value


def _replay_legacy_canonical_package(
    spools: dict[str, Path],
    *,
    manifest: dict[str, Any],
    signing_key: str | None,
) -> tuple[str, str | None]:
    checksum = hashlib.sha256()
    signature = hmac.new(signing_key.encode("utf-8"), digestmod=hashlib.sha256) if signing_key else None

    def update(content: bytes) -> None:
        checksum.update(content)
        if signature is not None:
            signature.update(content)

    update(b"{")
    for index, key in enumerate(sorted(spools)):
        if index:
            update(b",")
        update(canonical_json_bytes(key))
        update(b":")
        if key == "manifest":
            update(canonical_json_bytes(_legacy_manifest_for_digest(manifest)))
            continue
        with spools[key].open("rb", buffering=0) as file:
            while content := file.read(1024 * 1024):
                update(content)
    update(b"}")
    return checksum.hexdigest(), signature.hexdigest() if signature is not None else None


def _validate_legacy_integrity(
    spools: dict[str, Path],
    *,
    signing_key: str | None,
    require_signature: bool,
) -> tuple[dict[str, Any], bool | None, bool | None]:
    manifest_path = spools.get("manifest")
    if manifest_path is None:
        raise LegacyBackupValidationError("legacy backup manifest is missing")
    manifest = _load_small_spool(manifest_path, label="manifest")
    if not isinstance(manifest, dict):
        raise LegacyBackupValidationError("legacy backup manifest must be an object")
    schema = manifest.get("schema")
    if schema not in SUPPORTED_BACKUP_SCHEMAS:
        raise LegacyBackupValidationError(f"unsupported legacy backup schema: {schema!r}")
    actual_checksum, actual_signature = _replay_legacy_canonical_package(
        spools,
        manifest=manifest,
        signing_key=signing_key,
    )
    checksum_block = manifest.get("checksum")
    checksum_valid: bool | None = None
    if checksum_block is not None:
        checksum_valid = (
            isinstance(checksum_block, dict)
            and checksum_block.get("algorithm") == "sha256"
            and isinstance(checksum_block.get("value"), str)
            and hmac.compare_digest(checksum_block["value"], actual_checksum)
        )
        if not checksum_valid:
            raise LegacyBackupValidationError("legacy backup checksum mismatch")
    elif require_signature:
        raise LegacyBackupValidationError("legacy backup checksum is required but missing")

    signature_block = manifest.get("signature")
    signature_valid: bool | None = None
    if signature_block is not None:
        if not signing_key:
            if require_signature:
                raise LegacyBackupValidationError("legacy backup signature cannot be verified without a key")
        else:
            signature_valid = (
                isinstance(signature_block, dict)
                and signature_block.get("algorithm") == BACKUP_SIGNATURE_ALGORITHM
                and isinstance(signature_block.get("value"), str)
                and actual_signature is not None
                and hmac.compare_digest(signature_block["value"], actual_signature)
            )
            if not signature_valid:
                raise LegacyBackupValidationError("legacy backup signature mismatch")
    elif require_signature:
        raise LegacyBackupValidationError("legacy backup signature is required but missing")
    return manifest, checksum_valid, signature_valid


def _array_items(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("rb") as file:
        for item in ijson.items(file, "item", use_float=True):
            if not isinstance(item, dict):
                raise LegacyBackupValidationError(f"legacy section {path.name} contains a non-object item")
            yield item


def _check_required_fields(section: str, item: dict[str, Any], schema: str) -> None:
    fields = BackupService._REQUIRED_FIELDS.get(section, ())
    if schema in {BACKUP_SCHEMA_V2, BACKUP_SCHEMA}:
        fields = (*fields, *BackupService._V2_REQUIRED_FIELDS.get(section, ()))
    if schema == BACKUP_SCHEMA:
        fields = (*fields, *BackupService._V3_REQUIRED_FIELDS.get(section, ()))
    missing = [field for field in fields if field not in item]
    if missing:
        raise LegacyBackupValidationError(f"legacy {section} record is missing fields: {', '.join(missing)}")


def _legacy_media_index(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        PRAGMA journal_mode=OFF;
        PRAGMA synchronous=OFF;
        CREATE TABLE media(
            local_path TEXT PRIMARY KEY,
            staged_path TEXT NOT NULL,
            size INTEGER NOT NULL,
            checksum TEXT NOT NULL,
            consumed INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    return connection


def _decode_legacy_media_files(spool: Path | None, workspace: Path, index: sqlite3.Connection) -> int:
    if spool is None:
        return 0
    count = 0
    media_root = workspace / "legacy-media"
    media_root.mkdir(mode=0o700)
    for item in _array_items(spool):
        _check_required_fields("media_files", item, "chat-audit-core.backup.v1")
        local_path = item["local_path"]
        encoded = item["content_base64"]
        if not isinstance(local_path, str) or not isinstance(encoded, str):
            raise LegacyBackupValidationError("legacy embedded media has invalid path or content")
        expected_size = item["file_size"]
        checksum_block = item["file_checksum"]
        if not isinstance(checksum_block, dict) or checksum_block.get("algorithm") != "sha256":
            raise LegacyBackupValidationError(f"legacy embedded media checksum is invalid: {local_path}")
        expected_checksum = checksum_block.get("value")
        if not isinstance(expected_checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_checksum):
            raise LegacyBackupValidationError(f"legacy embedded media checksum value is invalid: {local_path}")
        staged_path = media_root / f"{count:08d}-{secrets.token_hex(4)}.bin"
        digest = hashlib.sha256()
        decoded_size = 0
        with staged_path.open("xb", buffering=0) as output:
            for offset in range(0, len(encoded), BASE64_DECODE_CHARS):
                block = encoded[offset : offset + BASE64_DECODE_CHARS]
                try:
                    decoded = base64.b64decode(block, validate=True)
                except (ValueError, TypeError) as exc:
                    raise LegacyBackupValidationError(f"legacy embedded media is not valid Base64: {local_path}") from exc
                output.write(decoded)
                digest.update(decoded)
                decoded_size += len(decoded)
            output.flush()
            os.fsync(output.fileno())
        if decoded_size != expected_size or digest.hexdigest() != expected_checksum:
            raise LegacyBackupValidationError(f"legacy embedded media size or checksum mismatch: {local_path}")
        try:
            index.execute(
                "INSERT INTO media(local_path, staged_path, size, checksum) VALUES (?, ?, ?, ?)",
                (local_path, str(staged_path), decoded_size, expected_checksum),
            )
        except sqlite3.IntegrityError as exc:
            raise LegacyBackupValidationError(f"duplicate legacy embedded media path: {local_path}") from exc
        count += 1
    index.commit()
    return count


def convert_legacy_backup_to_v4(
    legacy_path: Path,
    *,
    backup_root: Path,
    signing_key: str,
    system_id: str,
    require_legacy_signature: bool = False,
    chunk_bytes: int = 8 * 1024 * 1024,
    filename: str | None = None,
) -> BackupExportResult:
    created_at = utc_now()
    final_path = Path(backup_root) / (filename or backup_filename("converted", created_at))
    metadata = {
        "created_at": format_utc_z(created_at),
        "backup_type": "converted",
        "created_by": "legacy_streaming_converter",
        "source": {"system": "chat-audit-core", "instance_id": system_id},
        "filters": {},
        "database_snapshot": {"dialect": "legacy-json", "method": "streaming_conversion"},
    }
    with BackupArchiveWriter(
        final_path=final_path,
        metadata=metadata,
        signing_key=signing_key,
        key_id=system_id,
        chunk_bytes=chunk_bytes,
    ) as archive:
        spools: dict[str, Path] = {}
        media_index = _legacy_media_index(archive.workspace / "legacy-media-index.sqlite3")
        try:
            with _legacy_input(Path(legacy_path)) as source:
                spools, _item_counts = _spool_legacy_top_level(source, archive.workspace)
            manifest, checksum_valid, signature_valid = _validate_legacy_integrity(
                spools,
                signing_key=signing_key,
                require_signature=require_legacy_signature,
            )
            schema = str(manifest["schema"])
            archive.metadata["legacy"] = {
                "schema": schema,
                "checksum_valid": checksum_valid,
                "signature_valid": signature_valid,
                "source_filename": Path(legacy_path).name,
            }
            decoded_media_count = _decode_legacy_media_files(
                spools.get("media_files"),
                archive.workspace,
                media_index,
            )

            for section in LEGACY_SECTIONS:
                count = 0
                spool = spools.get(section)
                with archive.section(section) as output:
                    if spool is not None:
                        for item in _array_items(spool):
                            _check_required_fields(section, item, schema)
                            if section == "media_assets":
                                row = media_index.execute(
                                    "SELECT staged_path, size, checksum FROM media WHERE local_path = ?",
                                    (item.get("local_path"),),
                                ).fetchone()
                                if row is not None:
                                    staged_path, expected_size, expected_checksum = row
                                    media_info = archive.add_media(
                                        file_hash=str(item["file_hash"]),
                                        path=Path(staged_path),
                                    )
                                    if (
                                        media_info["archived_size"] != expected_size
                                        or media_info["file_checksum"]["value"] != expected_checksum
                                    ):
                                        raise LegacyBackupValidationError(
                                            f"legacy media changed during conversion: {item.get('local_path')}"
                                        )
                                    item.update(media_info)
                                    media_index.execute(
                                        "UPDATE media SET consumed = 1 WHERE local_path = ?",
                                        (item.get("local_path"),),
                                    )
                            output.write(item)
                            count += 1
                archive.set_count(section, count)
            unconsumed = media_index.execute("SELECT local_path FROM media WHERE consumed = 0 LIMIT 1").fetchone()
            if unconsumed is not None:
                raise LegacyBackupValidationError(
                    f"legacy embedded media has no matching media asset: {unconsumed[0]}"
                )
            archive.set_count("media_files", decoded_media_count)
            archive.set_count("missing_media_files", max(0, archive._counts.get("media_assets", 0) - decoded_media_count))
            path, new_manifest = archive.finalize()
            return BackupExportResult(path=path, manifest=new_manifest)
        finally:
            media_index.close()
