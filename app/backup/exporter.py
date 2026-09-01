from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.backup.archive import BackupArchiveWriter
from app.database import create_async_engine_and_sessionmaker
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
from app.time_utils import format_utc_z, utc_now


STREAM_BATCH_SIZE = 500


@dataclass(frozen=True)
class BackupExportResult:
    path: Path
    manifest: dict[str, Any]


def backup_filename(backup_type: str, created_at: dt.datetime | None = None) -> str:
    if backup_type not in {"auto", "manual", "export", "converted"}:
        raise ValueError(f"unsupported backup type: {backup_type!r}")
    timestamp = (created_at or utc_now()).strftime("%Y%m%dT%H%M%SZ")
    import secrets

    return f"{backup_type}-backup-{timestamp}-{secrets.token_hex(4)}.cacb"


def _copy_sqlite_database(source_path: Path, destination_path: Path) -> None:
    if not source_path.is_file():
        raise FileNotFoundError(f"SQLite database does not exist: {source_path}")
    source = sqlite3.connect(f"file:{source_path.as_posix()}?mode=ro", uri=True, timeout=30)
    destination = sqlite3.connect(destination_path)
    try:
        source.execute("PRAGMA busy_timeout=30000")
        source.backup(destination, pages=4096, sleep=0.02)
        destination.execute("PRAGMA journal_mode=DELETE")
        destination.execute("PRAGMA synchronous=FULL")
        destination.commit()
    finally:
        destination.close()
        source.close()
    descriptor = os.open(destination_path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


async def _write_scalar_section(
    db: AsyncSession,
    archive: BackupArchiveWriter,
    *,
    name: str,
    statement,
    serializer: Callable[[Any], dict[str, Any]],
) -> int:
    count = 0
    with archive.section(name) as section:
        result = await db.stream_scalars(statement.execution_options(yield_per=STREAM_BATCH_SIZE))
        async for record in result:
            section.write(serializer(record))
            count += 1
    archive.set_count(name, count)
    return count


async def _write_message_parts(db: AsyncSession, archive: BackupArchiveWriter) -> int:
    statement = (
        select(MessagePart, MessageMediaReference.ordinal.label("reference_ordinal"))
        .outerjoin(MessageMediaReference, MessageMediaReference.id == MessagePart.media_reference_id)
        .order_by(MessagePart.msg_hash.asc(), MessagePart.ordinal.asc())
    )
    count = 0
    with archive.section("message_parts") as section:
        result = await db.stream(statement.execution_options(yield_per=STREAM_BATCH_SIZE))
        async for part, reference_ordinal in result:
            item = BackupService._message_part_to_dict(
                part,
                {part.media_reference_id: reference_ordinal}
                if part.media_reference_id is not None and reference_ordinal is not None
                else {},
            )
            section.write(item)
            count += 1
    archive.set_count("message_parts", count)
    return count


async def _write_media_assets(
    db: AsyncSession,
    archive: BackupArchiveWriter,
    *,
    storage_root: Path,
    public_storage_prefix: str,
) -> tuple[int, int, int]:
    asset_count = 0
    media_file_count = 0
    missing_file_count = 0
    statement = select(MediaAsset).order_by(MediaAsset.file_hash.asc())
    with archive.section("media_assets") as section:
        result = await db.stream_scalars(statement.execution_options(yield_per=STREAM_BATCH_SIZE))
        async for asset in result:
            item = BackupService._media_asset_to_dict(asset)
            file_path = BackupService._local_media_file_path(
                asset.local_path,
                storage_root,
                public_storage_prefix,
            )
            if file_path is not None and file_path.is_file():
                item.update(archive.add_media(file_hash=asset.file_hash, path=file_path))
                media_file_count += 1
            else:
                missing_file_count += 1
            section.write(item)
            asset_count += 1
    archive.set_count("media_assets", asset_count)
    archive.set_count("media_files", media_file_count)
    archive.set_count("missing_media_files", missing_file_count)
    return asset_count, media_file_count, missing_file_count


async def export_full_database_snapshot(
    db: AsyncSession,
    archive: BackupArchiveWriter,
    *,
    storage_root: Path,
    public_storage_prefix: str,
    progress_hook: Callable[[str], Awaitable[None]] | None = None,
) -> None:
    """Stream every v3 logical data section from one already-fixed snapshot."""

    await _write_scalar_section(
        db,
        archive,
        name="import_sources",
        statement=select(ImportSource).order_by(ImportSource.id.asc()),
        serializer=BackupService._import_source_to_dict,
    )
    await _write_scalar_section(
        db,
        archive,
        name="import_batches",
        statement=select(ImportBatch).order_by(ImportBatch.started_at.asc(), ImportBatch.id.asc()),
        serializer=BackupService._import_batch_to_dict,
    )
    await _write_scalar_section(
        db,
        archive,
        name="messages",
        statement=select(Message).order_by(Message.timestamp.asc(), Message.msg_hash.asc()),
        serializer=BackupService._message_to_dict,
    )
    if progress_hook is not None:
        await progress_hook("messages")
    await _write_scalar_section(
        db,
        archive,
        name="robot_messages",
        statement=select(RobotMessage).order_by(RobotMessage.robot_id.asc(), RobotMessage.msg_hash.asc()),
        serializer=lambda record: {"robot_id": record.robot_id, "msg_hash": record.msg_hash},
    )
    await _write_scalar_section(
        db,
        archive,
        name="message_source_records",
        statement=select(MessageSourceRecord).order_by(
            MessageSourceRecord.msg_hash.asc(),
            MessageSourceRecord.source_id.asc(),
            MessageSourceRecord.id.asc(),
        ),
        serializer=BackupService._message_source_record_to_dict,
    )
    await _write_scalar_section(
        db,
        archive,
        name="message_media_references",
        statement=select(MessageMediaReference).order_by(
            MessageMediaReference.msg_hash.asc(),
            MessageMediaReference.ordinal.asc(),
        ),
        serializer=BackupService._message_media_reference_to_dict,
    )
    await _write_message_parts(db, archive)
    await _write_scalar_section(
        db,
        archive,
        name="identity_aliases",
        statement=select(IdentityAlias).order_by(
            IdentityAlias.identity_type.asc(),
            IdentityAlias.canonical_id.asc(),
            IdentityAlias.alias_type.asc(),
            IdentityAlias.alias_id.asc(),
            IdentityAlias.id.asc(),
        ),
        serializer=BackupService._identity_alias_to_dict,
    )
    await _write_scalar_section(
        db,
        archive,
        name="profile_change_records",
        statement=select(ProfileChangeRecord).order_by(
            ProfileChangeRecord.observed_at.asc(),
            ProfileChangeRecord.id.asc(),
        ),
        serializer=BackupService._profile_change_record_to_dict,
    )
    await _write_scalar_section(
        db,
        archive,
        name="room_profiles",
        statement=select(RoomProfile).order_by(RoomProfile.room_id.asc()),
        serializer=BackupService._room_profile_to_dict,
    )
    await _write_scalar_section(
        db,
        archive,
        name="user_profiles",
        statement=select(UserProfile).order_by(UserProfile.user_id.asc()),
        serializer=BackupService._user_profile_to_dict,
    )
    await _write_media_assets(
        db,
        archive,
        storage_root=storage_root,
        public_storage_prefix=public_storage_prefix,
    )


async def _export_from_engine(
    engine: AsyncEngine,
    archive: BackupArchiveWriter,
    *,
    storage_root: Path,
    public_storage_prefix: str,
    postgres_repeatable_read: bool,
    progress_hook: Callable[[str], Awaitable[None]] | None = None,
) -> None:
    async with engine.connect() as connection:
        if postgres_repeatable_read:
            connection = await connection.execution_options(isolation_level="REPEATABLE READ")
        async with connection.begin():
            if postgres_repeatable_read:
                await connection.execute(text("SET TRANSACTION READ ONLY"))
            async with AsyncSession(bind=connection, expire_on_commit=False) as db:
                await export_full_database_snapshot(
                    db,
                    archive,
                    storage_root=storage_root,
                    public_storage_prefix=public_storage_prefix,
                    progress_hook=progress_hook,
                )


async def create_full_backup_archive(
    *,
    database_url: str,
    storage_root: Path,
    backup_root: Path,
    public_storage_prefix: str,
    signing_key: str,
    system_id: str,
    backup_type: str,
    created_by: str,
    filename: str | None = None,
    chunk_bytes: int = 8 * 1024 * 1024,
    min_free_bytes: int = 512 * 1024 * 1024,
    progress_hook: Callable[[str], Awaitable[None]] | None = None,
) -> BackupExportResult:
    created_at = utc_now()
    parsed_url = make_url(database_url)
    dialect = parsed_url.get_backend_name()
    if dialect not in {"sqlite", "postgresql"}:
        raise ValueError(f"unsupported backup database dialect: {dialect!r}")
    backup_root = Path(backup_root)
    backup_root.mkdir(parents=True, exist_ok=True)
    final_path = backup_root / (filename or backup_filename(backup_type, created_at))
    metadata = {
        "created_at": format_utc_z(created_at),
        "backup_type": backup_type,
        "created_by": created_by,
        "source": {"system": "chat-audit-core", "instance_id": system_id},
        "filters": {
            "robot_id": None,
            "room_id": None,
            "message_type": None,
            "start_timestamp": None,
            "end_timestamp": None,
        },
        "database_snapshot": {
            "dialect": dialect,
            "method": "sqlite_online_backup" if dialect == "sqlite" else "repeatable_read_read_only",
        },
    }

    with BackupArchiveWriter(
        final_path=final_path,
        metadata=metadata,
        signing_key=signing_key,
        key_id=system_id,
        chunk_bytes=chunk_bytes,
        min_free_bytes=min_free_bytes,
    ) as archive:
        engine = None
        try:
            export_url = database_url
            if dialect == "sqlite":
                database_name = parsed_url.database
                if database_name in {None, "", ":memory:"}:
                    raise ValueError("worker backups require a file-backed SQLite database")
                source_path = Path(database_name)
                if not source_path.is_absolute():
                    source_path = source_path.resolve()
                snapshot_path = archive.workspace / "database-snapshot.sqlite3"
                await asyncio.to_thread(_copy_sqlite_database, source_path, snapshot_path)
                export_url = f"sqlite+aiosqlite:///{snapshot_path.as_posix()}"

            engine, _sessionmaker = create_async_engine_and_sessionmaker(export_url)
            await _export_from_engine(
                engine,
                archive,
                storage_root=Path(storage_root),
                public_storage_prefix=public_storage_prefix,
                postgres_repeatable_read=dialect == "postgresql",
                progress_hook=progress_hook,
            )
            path, manifest = archive.finalize()
            return BackupExportResult(path=path, manifest=manifest)
        finally:
            if engine is not None:
                with contextlib.suppress(Exception):
                    await engine.dispose()
