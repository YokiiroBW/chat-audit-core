from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator

from sqlalchemy import insert, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.backup.archive import ArchiveValidationReport, COPY_CHUNK_BYTES, validate_backup_archive
from app.database import create_all_tables, create_async_engine_and_sessionmaker
from app.models import (
    IdentityAlias,
    ImportBatch,
    ImportSource,
    MediaAsset,
    Message,
    MessageMediaReference,
    MessagePart,
    MessageSourceRecord,
    ProfileChangeRecord,
    RobotMessage,
    RoomProfile,
    UserProfile,
)
from app.services.backup_service import BackupService
from app.time_utils import parse_utc_datetime, utc_now


RESTORE_BATCH_SIZE = 250
RESTORE_MODELS = (
    ImportSource,
    ImportBatch,
    Message,
    RobotMessage,
    MediaAsset,
    MessageSourceRecord,
    MessageMediaReference,
    MessagePart,
    IdentityAlias,
    ProfileChangeRecord,
    RoomProfile,
    UserProfile,
)
SECTION_ORDER = (
    "import_sources",
    "import_batches",
    "messages",
    "robot_messages",
    "media_assets",
    "message_source_records",
    "message_media_references",
    "message_parts",
    "identity_aliases",
    "profile_change_records",
    "room_profiles",
    "user_profiles",
)


@dataclass(frozen=True)
class RestoreResult:
    counts: dict[str, int]
    media_files_created: int
    media_files_reused: int
    validation: ArchiveValidationReport


def _json_text(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _section_records(extracted_root: Path, section: str) -> Iterator[dict[str, Any]]:
    section_root = extracted_root / "db" / section
    if not section_root.exists():
        return
    for path in sorted(section_root.glob("*.jsonl")):
        with path.open("rb") as file:
            for line in file:
                record = json.loads(line)
                if not isinstance(record, dict):
                    raise ValueError(f"non-object restore record in {path.name}")
                yield record


def _batches(records: Iterable[dict[str, Any]], size: int = RESTORE_BATCH_SIZE) -> Iterator[list[dict[str, Any]]]:
    batch: list[dict[str, Any]] = []
    for record in records:
        batch.append(record)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def _source_mapping(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item["id"],
        "source_type": item["source_type"],
        "platform": item["platform"],
        "account_id": item["account_id"],
        "device_id": item["device_id"],
        "device_name": item.get("device_name"),
        "qq_version": item.get("qq_version"),
        "schema_version": item.get("schema_version"),
        "status": item["status"],
        "first_seen_at": parse_utc_datetime(item["first_seen_at"]),
        "last_seen_at": parse_utc_datetime(item["last_seen_at"]),
        "metadata_json": _json_text(item.get("metadata")),
    }


def _batch_mapping(item: dict[str, Any]) -> dict[str, Any]:
    mapping = {
        "id": item["id"],
        "source_id": item["source_id"],
        "mode": item["mode"],
        "status": item["status"],
        "started_at": parse_utc_datetime(item["started_at"]),
        "completed_at": parse_utc_datetime(item["completed_at"]) if item.get("completed_at") else None,
        "detail_json": _json_text(item.get("detail")),
    }
    for field in (
        "scanned_messages",
        "inserted_messages",
        "updated_messages",
        "skipped_messages",
        "uploaded_media",
        "not_downloaded_media",
        "missing_media",
        "failed_media",
    ):
        mapping[field] = int(item.get(field) or 0)
    return mapping


def _message_mapping(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "msg_hash": item["msg_hash"],
        "platform": item["platform"],
        "room_id": item["room_id"],
        "message_type": item["message_type"],
        "external_message_id": item.get("external_message_id"),
        "sender_id": item["sender_id"],
        "nickname": item.get("nickname"),
        "is_outgoing": item.get("is_outgoing"),
        "raw_message": item["raw_message"],
        "local_message": item["local_message"],
        "timestamp": item["timestamp"],
        "source_sequence": item.get("source_sequence"),
        "created_at": parse_utc_datetime(item["created_at"]) if item.get("created_at") else utc_now(),
    }


def _media_asset_mapping(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "file_hash": item["file_hash"],
        "content_sha256": item.get("content_sha256"),
        "file_type": item["file_type"],
        "file_size": item["file_size"],
        "local_path": item["local_path"],
        "created_at": parse_utc_datetime(item["created_at"]) if item.get("created_at") else utc_now(),
    }


def _source_record_mapping(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "msg_hash": item["msg_hash"],
        "source_id": item["source_id"],
        "batch_id": item.get("batch_id"),
        "source_table": item["source_table"],
        "source_primary_key": item["source_primary_key"],
        "source_external_message_id": item.get("source_external_message_id"),
        "platform_message_id": item.get("platform_message_id"),
        "schema_version": item.get("schema_version"),
        "raw_columns_json": _json_text(item.get("raw_columns")),
        "raw_40800_protobuf": item.get("raw_40800_protobuf"),
        "raw_40900_protobuf": item.get("raw_40900_protobuf"),
        "metadata_json": _json_text(item.get("metadata")),
        "imported_at": parse_utc_datetime(item["imported_at"]),
        "last_seen_at": parse_utc_datetime(item["last_seen_at"]),
    }


def _media_reference_mapping(item: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "msg_hash",
        "ordinal",
        "media_type",
        "source_state",
        "archive_state",
        "asset_file_hash",
        "thumbnail_file_hash",
        "file_name",
        "file_ext",
        "declared_file_size",
        "actual_file_size",
        "source_md5",
        "source_sha1",
        "source_uuid",
        "content_sha256",
        "source_local_path",
        "duration_ms",
        "width",
        "height",
        "failure_code",
        "failure_detail",
    )
    mapping = {field: item.get(field) for field in fields}
    mapping.update(
        {
            "metadata_json": _json_text(item.get("metadata")),
            "first_seen_at": parse_utc_datetime(item["first_seen_at"]),
            "last_checked_at": parse_utc_datetime(item["last_checked_at"]),
            "archived_at": parse_utc_datetime(item["archived_at"]) if item.get("archived_at") else None,
        }
    )
    return mapping


def _identity_alias_mapping(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "platform": item["platform"],
        "identity_type": item["identity_type"],
        "canonical_id": item["canonical_id"],
        "alias_id": item["alias_id"],
        "alias_type": item["alias_type"],
        "source_id": item.get("source_id"),
        "confidence": item["confidence"],
        "metadata_json": _json_text(item.get("metadata")),
        "first_seen_at": parse_utc_datetime(item["first_seen_at"]),
        "last_seen_at": parse_utc_datetime(item["last_seen_at"]),
    }


def _profile_change_mapping(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "identity_type": item["identity_type"],
        "identity_id": item["identity_id"],
        "old_display_name": item.get("old_display_name"),
        "new_display_name": item.get("new_display_name"),
        "old_avatar_file_hash": item.get("old_avatar_file_hash"),
        "new_avatar_file_hash": item.get("new_avatar_file_hash"),
        "source_id": item.get("source_id"),
        "observed_at": parse_utc_datetime(item["observed_at"]),
    }


def _room_profile_mapping(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "room_id": item["room_id"],
        "platform": item["platform"],
        "display_name": item.get("display_name"),
        "avatar_path": item.get("avatar_path"),
        "avatar_file_hash": item.get("avatar_file_hash"),
        "avatar_source_url": item.get("avatar_source_url"),
        "avatar_status": item.get("avatar_status") or "unknown",
        "updated_at": parse_utc_datetime(item["updated_at"]) if item.get("updated_at") else utc_now(),
    }


def _user_profile_mapping(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "user_id": item["user_id"],
        "platform": item["platform"],
        "display_name": item.get("display_name"),
        "avatar_path": item.get("avatar_path"),
        "avatar_file_hash": item.get("avatar_file_hash"),
        "avatar_source_url": item.get("avatar_source_url"),
        "avatar_status": item.get("avatar_status") or "unknown",
        "updated_at": parse_utc_datetime(item["updated_at"]) if item.get("updated_at") else utc_now(),
    }


SECTION_MODELS_AND_MAPPERS = {
    "import_sources": (ImportSource, _source_mapping),
    "import_batches": (ImportBatch, _batch_mapping),
    "messages": (Message, _message_mapping),
    "robot_messages": (RobotMessage, lambda item: {"robot_id": item["robot_id"], "msg_hash": item["msg_hash"]}),
    "media_assets": (MediaAsset, _media_asset_mapping),
    "message_source_records": (MessageSourceRecord, _source_record_mapping),
    "message_media_references": (MessageMediaReference, _media_reference_mapping),
    "identity_aliases": (IdentityAlias, _identity_alias_mapping),
    "profile_change_records": (ProfileChangeRecord, _profile_change_mapping),
    "room_profiles": (RoomProfile, _room_profile_mapping),
    "user_profiles": (UserProfile, _user_profile_mapping),
}


def _media_journal(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        PRAGMA journal_mode=OFF;
        PRAGMA synchronous=OFF;
        CREATE TABLE media(
            archive_member TEXT PRIMARY KEY,
            local_path TEXT NOT NULL,
            size INTEGER NOT NULL,
            checksum TEXT NOT NULL,
            published_path TEXT,
            created INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    return connection


def _record_media_mapping(journal: sqlite3.Connection, item: dict[str, Any]) -> None:
    member = item.get("archive_member")
    if member is None:
        return
    checksum = item.get("file_checksum") or {}
    journal.execute(
        "INSERT INTO media(archive_member, local_path, size, checksum) VALUES (?, ?, ?, ?)",
        (member, item["local_path"], item["archived_size"], checksum["value"]),
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb", buffering=0) as file:
        while content := file.read(COPY_CHUNK_BYTES):
            digest.update(content)
    return digest.hexdigest()


def _copy_media_atomically(source: Path, destination: Path, expected_size: int, expected_checksum: str) -> bool:
    if destination.exists():
        if destination.is_file() and destination.stat().st_size == expected_size and _sha256_file(destination) == expected_checksum:
            return False
        raise ValueError(f"restore destination already exists with different content: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.parent / f".{destination.name}.{secrets.token_hex(8)}.restore-incomplete"
    try:
        digest = hashlib.sha256()
        copied = 0
        with source.open("rb", buffering=0) as input_file, temporary.open("xb", buffering=0) as output_file:
            while content := input_file.read(COPY_CHUNK_BYTES):
                output_file.write(content)
                digest.update(content)
                copied += len(content)
            output_file.flush()
            os.fsync(output_file.fileno())
        if copied != expected_size or digest.hexdigest() != expected_checksum:
            raise ValueError(f"staged restore media checksum mismatch: {source.name}")
        os.replace(temporary, destination)
        return True
    finally:
        temporary.unlink(missing_ok=True)


def _publish_media(
    journal: sqlite3.Connection,
    *,
    extracted_root: Path,
    storage_root: Path,
    public_storage_prefix: str,
) -> tuple[int, int]:
    created = 0
    reused = 0
    for member, local_path, size, checksum in journal.execute(
        "SELECT archive_member, local_path, size, checksum FROM media ORDER BY archive_member"
    ):
        source = extracted_root / member
        destination = BackupService._local_media_file_path(local_path, storage_root, public_storage_prefix)
        if destination is None:
            raise ValueError(f"invalid restore media path: {local_path!r}")
        was_created = _copy_media_atomically(source, destination, size, checksum)
        journal.execute(
            "UPDATE media SET published_path = ?, created = ? WHERE archive_member = ?",
            (str(destination), 1 if was_created else 0, member),
        )
        journal.commit()
        if was_created:
            created += 1
        else:
            reused += 1
    return created, reused


def _rollback_published_media(journal: sqlite3.Connection) -> None:
    for (raw_path,) in journal.execute("SELECT published_path FROM media WHERE created = 1 ORDER BY archive_member DESC"):
        if raw_path:
            Path(raw_path).unlink(missing_ok=True)


async def _assert_restore_target_empty(db: AsyncSession) -> None:
    for model in RESTORE_MODELS:
        if (await db.execute(select(model).limit(1))).first() is not None:
            raise ValueError(f"v4 full restore target table is not empty: {model.__tablename__}")


async def _insert_regular_section(
    db: AsyncSession,
    *,
    extracted_root: Path,
    section: str,
    journal: sqlite3.Connection,
) -> int:
    model, mapper = SECTION_MODELS_AND_MAPPERS[section]
    count = 0
    for items in _batches(_section_records(extracted_root, section)):
        if section == "media_assets":
            for item in items:
                _record_media_mapping(journal, item)
            journal.commit()
        mappings = [mapper(item) for item in items]
        await db.execute(insert(model), mappings)
        count += len(mappings)
    return count


async def _insert_message_parts(db: AsyncSession, *, extracted_root: Path) -> int:
    count = 0
    for items in _batches(_section_records(extracted_root, "message_parts"), size=150):
        keys = [
            (item["msg_hash"], item["media_reference_ordinal"])
            for item in items
            if item.get("media_reference_ordinal") is not None
        ]
        references: dict[tuple[str, int], int] = {}
        if keys:
            result = await db.execute(
                select(
                    MessageMediaReference.id,
                    MessageMediaReference.msg_hash,
                    MessageMediaReference.ordinal,
                ).where(tuple_(MessageMediaReference.msg_hash, MessageMediaReference.ordinal).in_(keys))
            )
            references = {(msg_hash, ordinal): reference_id for reference_id, msg_hash, ordinal in result}
        mappings = []
        for item in items:
            ordinal = item.get("media_reference_ordinal")
            media_reference_id = None
            if ordinal is not None:
                key = (item["msg_hash"], ordinal)
                if key not in references:
                    raise ValueError(f"message part references missing media ordinal: {key}")
                media_reference_id = references[key]
            mappings.append(
                {
                    "msg_hash": item["msg_hash"],
                    "ordinal": item["ordinal"],
                    "part_type": item["part_type"],
                    "text_content": item.get("text_content"),
                    "media_reference_id": media_reference_id,
                    "payload_json": _json_text(item.get("payload")),
                    "source_format": item.get("source_format"),
                    "render_status": item["render_status"],
                }
            )
        await db.execute(insert(MessagePart), mappings)
        count += len(mappings)
    return count


async def restore_backup_archive(
    archive_path: Path,
    *,
    database_url: str,
    storage_root: Path,
    public_storage_prefix: str,
    signing_key: str,
    work_root: Path | None = None,
    cleanup_work_root: bool = True,
) -> RestoreResult:
    """Validate and restore a full v4 archive into an empty isolated target."""

    archive_path = Path(archive_path)
    storage_root = Path(storage_root)
    storage_root.mkdir(parents=True, exist_ok=True)
    parent = Path(work_root) if work_root is not None else archive_path.parent
    parent.mkdir(parents=True, exist_ok=True)
    extracted_root = parent / f".restore-{secrets.token_hex(8)}"
    validation = await asyncio.to_thread(
        validate_backup_archive,
        archive_path,
        signing_key=signing_key,
        require_signature=True,
        extract_root=extracted_root,
    )
    journal = _media_journal(extracted_root / "restore-media.sqlite3")
    engine, sessionmaker = create_async_engine_and_sessionmaker(database_url)
    created = 0
    reused = 0
    counts: dict[str, int] = {}
    try:
        await create_all_tables(engine)
        async with sessionmaker() as db:
            try:
                async with db.begin():
                    await _assert_restore_target_empty(db)
                    for section in SECTION_ORDER:
                        if section == "message_parts":
                            counts[section] = await _insert_message_parts(db, extracted_root=extracted_root)
                        else:
                            counts[section] = await _insert_regular_section(
                                db,
                                extracted_root=extracted_root,
                                section=section,
                                journal=journal,
                            )
                    created, reused = _publish_media(
                        journal,
                        extracted_root=extracted_root,
                        storage_root=storage_root,
                        public_storage_prefix=public_storage_prefix,
                    )
                    counts["media_files"] = created + reused
                    counts["missing_media_files"] = max(0, counts.get("media_assets", 0) - counts["media_files"])
                    expected_counts = validation.manifest.get("counts") or {}
                    if counts != expected_counts:
                        raise ValueError(
                            f"restored counts do not match manifest: restored={counts!r} expected={expected_counts!r}"
                        )
            except BaseException:
                _rollback_published_media(journal)
                raise
        return RestoreResult(
            counts=counts,
            media_files_created=created,
            media_files_reused=reused,
            validation=validation,
        )
    finally:
        journal.close()
        await engine.dispose()
        if cleanup_work_root:
            shutil.rmtree(extracted_root, ignore_errors=True)
