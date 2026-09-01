from __future__ import annotations

import asyncio
import errno
import io
import json
import os
import sqlite3
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import pytest
from sqlalchemy import func, insert, select

from app.backup.archive import ArchiveValidationError, BackupArchiveWriter, validate_backup_archive
from app.backup.exporter import create_full_backup_archive
from app.backup.jobs import BackupAlreadyRunningError, backup_execution_lock, enqueue_backup_job, get_backup_job
from app.backup.legacy import convert_legacy_backup_to_v4
from app.backup.restore import restore_backup_archive
from app.backup.worker import _heartbeat_during_job, apply_auto_backup_retention, run_one_job
from app.config import Settings
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


async def _source_database(root: Path, *, media_content: bytes = b"media-content"):
    root.mkdir(parents=True, exist_ok=True)
    storage = root / "storage"
    storage.mkdir()
    (storage / "asset.bin").write_bytes(media_content)
    database_url = f"sqlite+aiosqlite:///{root / 'source.sqlite3'}"
    engine, sessionmaker = create_async_engine_and_sessionmaker(database_url)
    await create_all_tables(engine)
    async with sessionmaker() as db:
        db.add_all(
            [
                Message(
                    msg_hash="message-one",
                    platform="qq",
                    room_id="room-one",
                    message_type="group",
                    sender_id="user-one",
                    raw_message="hello",
                    local_message="[CQ:file,file=/static/storage/asset.bin]",
                    timestamp=1,
                ),
                RobotMessage(robot_id="robot-one", msg_hash="message-one"),
                MediaAsset(
                    file_hash="asset-one",
                    file_type="file",
                    file_size=len(media_content),
                    local_path="/static/storage/asset.bin",
                ),
            ]
        )
        await db.commit()
    return database_url, storage, engine, sessionmaker


@pytest.mark.asyncio
async def test_v4_full_backup_validates_and_restores_to_isolated_sqlite(tmp_path):
    media = (b"bounded-media-" * 10000) + b"end"
    database_url, storage, source_engine, _ = await _source_database(tmp_path / "source", media_content=media)
    backup = await create_full_backup_archive(
        database_url=database_url,
        storage_root=storage,
        backup_root=tmp_path / "backups",
        public_storage_prefix="/static/storage",
        signing_key="test-signing-key",
        system_id="test-instance",
        backup_type="manual",
        created_by="pytest",
        chunk_bytes=1024,
        min_free_bytes=0,
    )

    validation = validate_backup_archive(backup.path, signing_key="test-signing-key")
    restore = await restore_backup_archive(
        backup.path,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'restore.sqlite3'}",
        storage_root=tmp_path / "restore-media",
        public_storage_prefix="/static/storage",
        signing_key="test-signing-key",
    )

    restored_engine, restored_sessions = create_async_engine_and_sessionmaker(
        f"sqlite+aiosqlite:///{tmp_path / 'restore.sqlite3'}"
    )
    async with restored_sessions() as db:
        message_count = (await db.execute(select(func.count()).select_from(Message))).scalar_one()
        robot_count = (await db.execute(select(func.count()).select_from(RobotMessage))).scalar_one()
        asset_count = (await db.execute(select(func.count()).select_from(MediaAsset))).scalar_one()
    assert validation.valid is True
    assert validation.checksum_valid is True
    assert validation.signature_valid is True
    assert restore.counts == validation.manifest["counts"]
    assert (message_count, robot_count, asset_count) == (1, 1, 1)
    assert restore.media_files_created == 1
    assert (tmp_path / "restore-media" / "asset.bin").read_bytes() == media
    await restored_engine.dispose()
    await source_engine.dispose()


@pytest.mark.asyncio
async def test_v4_backup_and_restore_include_current_and_legacy_media_prefixes(tmp_path):
    database_url, storage, source_engine, source_sessions = await _source_database(tmp_path / "source")
    current_content = b"current-prefix-media"
    (storage / "current.bin").write_bytes(current_content)
    async with source_sessions() as db:
        db.add(
            MediaAsset(
                file_hash="asset-current",
                file_type="file",
                file_size=len(current_content),
                local_path="/media/current.bin",
            )
        )
        await db.commit()

    backup = await create_full_backup_archive(
        database_url=database_url,
        storage_root=storage,
        backup_root=tmp_path / "backups",
        public_storage_prefix="/media",
        signing_key="test-signing-key",
        system_id="test-instance",
        backup_type="manual",
        created_by="pytest",
        min_free_bytes=0,
    )
    validation = validate_backup_archive(backup.path, signing_key="test-signing-key")
    restored = await restore_backup_archive(
        backup.path,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'restore.sqlite3'}",
        storage_root=tmp_path / "restore-media",
        public_storage_prefix="/media",
        signing_key="test-signing-key",
    )

    assert validation.counts["media_assets"] == 2
    assert validation.counts["media_files"] == 2
    assert validation.counts["missing_media_files"] == 0
    assert restored.media_files_created == 2
    assert (tmp_path / "restore-media" / "asset.bin").read_bytes() == b"media-content"
    assert (tmp_path / "restore-media" / "current.bin").read_bytes() == current_content
    assert BackupService._local_media_file_path("/media/current.bin", storage, "/static/storage") == (
        storage / "current.bin"
    ).resolve()
    assert BackupService._local_media_file_path("/other/current.bin", storage, "/media") is None
    assert BackupService._local_media_file_path("/media/../outside.bin", storage, "/media") is None
    await source_engine.dispose()


@pytest.mark.asyncio
async def test_v4_restore_preserves_all_logical_sections_and_media_relationships(tmp_path):
    database_url, storage, source_engine, source_sessions = await _source_database(tmp_path / "source")
    async with source_sessions() as db:
        source = ImportSource(
            id="source-one",
            source_type="qqnt_local_db",
            platform="qq",
            account_id="robot-one",
            device_id="device-one",
            status="active",
        )
        batch = ImportBatch(
            id="batch-one",
            source_id=source.id,
            mode="initial",
            status="completed",
            scanned_messages=1,
            inserted_messages=1,
        )
        db.add_all([source, batch])
        reference = MessageMediaReference(
            msg_hash="message-one",
            ordinal=0,
            media_type="file",
            source_state="downloaded",
            archive_state="complete",
            asset_file_hash="asset-one",
        )
        db.add(reference)
        await db.flush()
        db.add_all(
            [
                MessageSourceRecord(
                    msg_hash="message-one",
                    source_id=source.id,
                    batch_id=batch.id,
                    source_table="messages",
                    source_primary_key="source-primary-one",
                ),
                MessagePart(
                    msg_hash="message-one",
                    ordinal=0,
                    part_type="file",
                    media_reference_id=reference.id,
                    payload_json='{"name":"asset.bin"}',
                    source_format="onebot11",
                    render_status="parsed",
                ),
                IdentityAlias(
                    platform="qq",
                    identity_type="conversation",
                    canonical_id="room-one",
                    alias_id="legacy-room-one",
                    alias_type="qqnt_peer_id",
                    source_id=source.id,
                    confidence="verified",
                ),
                ProfileChangeRecord(
                    identity_type="conversation",
                    identity_id="room-one",
                    new_display_name="Room One",
                    new_avatar_file_hash="asset-one",
                    source_id=source.id,
                ),
                RoomProfile(
                    room_id="room-one",
                    platform="qq",
                    display_name="Room One",
                    avatar_path="/static/storage/asset.bin",
                    avatar_file_hash="asset-one",
                    avatar_status="cached",
                ),
                UserProfile(
                    user_id="user-one",
                    platform="qq",
                    display_name="User One",
                    avatar_path="/static/storage/asset.bin",
                    avatar_file_hash="asset-one",
                    avatar_status="cached",
                ),
            ]
        )
        await db.commit()

    backup = await create_full_backup_archive(
        database_url=database_url,
        storage_root=storage,
        backup_root=tmp_path / "backups",
        public_storage_prefix="/static/storage",
        signing_key="test-signing-key",
        system_id="test-instance",
        backup_type="manual",
        created_by="pytest",
        chunk_bytes=512,
        min_free_bytes=0,
    )
    restore_url = f"sqlite+aiosqlite:///{tmp_path / 'restore.sqlite3'}"
    restored = await restore_backup_archive(
        backup.path,
        database_url=restore_url,
        storage_root=tmp_path / "restore-media",
        public_storage_prefix="/static/storage",
        signing_key="test-signing-key",
    )

    restored_engine, restored_sessions = create_async_engine_and_sessionmaker(restore_url)
    async with restored_sessions() as db:
        part = (await db.execute(select(MessagePart))).scalar_one()
        restored_reference = await db.get(MessageMediaReference, part.media_reference_id)
        source_record = (await db.execute(select(MessageSourceRecord))).scalar_one()
        alias = (await db.execute(select(IdentityAlias))).scalar_one()
        change = (await db.execute(select(ProfileChangeRecord))).scalar_one()
        room = await db.get(RoomProfile, "room-one")
        user = await db.get(UserProfile, "user-one")

    assert all(restored.counts[section] == 1 for section in (
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
        "media_files",
    ))
    assert restored_reference.msg_hash == part.msg_hash == "message-one"
    assert restored_reference.ordinal == 0
    assert source_record.source_id == alias.source_id == change.source_id == "source-one"
    assert source_record.batch_id == "batch-one"
    assert room.avatar_file_hash == user.avatar_file_hash == "asset-one"
    assert (tmp_path / "restore-media" / "asset.bin").read_bytes() == b"media-content"
    await restored_engine.dispose()
    await source_engine.dispose()


@pytest.mark.asyncio
async def test_v4_checksum_and_hmac_detect_tampering(tmp_path):
    database_url, storage, engine, _ = await _source_database(tmp_path / "source")
    backup = await create_full_backup_archive(
        database_url=database_url,
        storage_root=storage,
        backup_root=tmp_path / "backups",
        public_storage_prefix="/static/storage",
        signing_key="correct-key",
        system_id="test-instance",
        backup_type="manual",
        created_by="pytest",
        min_free_bytes=0,
    )
    with pytest.raises(ArchiveValidationError, match="signature"):
        validate_backup_archive(backup.path, signing_key="wrong-key")

    tampered = tmp_path / "tampered.cacb"
    with tarfile.open(backup.path, "r:gz") as source, tarfile.open(tampered, "w:gz") as destination:
        for member in source.getmembers():
            content = source.extractfile(member).read()
            if member.name.startswith("db/messages/"):
                content = content.replace(b"message-one", b"message-tw0", 1)
            replacement = tarfile.TarInfo(member.name)
            replacement.size = len(content)
            replacement.mode = 0o600
            destination.addfile(replacement, io.BytesIO(content))
    with pytest.raises(ArchiveValidationError, match="checksum"):
        validate_backup_archive(tampered, signing_key="correct-key")
    await engine.dispose()


def test_v4_disk_full_and_interruption_never_publish_a_partial_backup(tmp_path, monkeypatch):
    existing = tmp_path / "auto-backup-existing.cacb"
    with BackupArchiveWriter(
        final_path=existing,
        metadata={"created_at": "2026-09-01T00:00:00Z", "backup_type": "auto"},
        signing_key="key",
        key_id="id",
        min_free_bytes=0,
    ) as existing_writer:
        with existing_writer.section("messages") as section:
            section.write({"msg_hash": "known-good"})
        existing_writer.set_count("messages", 1)
        existing_writer.finalize()
    assert validate_backup_archive(existing, signing_key="key").valid is True
    target = tmp_path / "manual-backup-new.cacb"

    usage = os.statvfs(tmp_path)
    disk_usage_type = type(__import__("shutil").disk_usage(tmp_path))
    monkeypatch.setattr(
        "app.backup.archive.shutil.disk_usage",
        lambda _path: disk_usage_type(total=usage.f_blocks, used=0, free=1),
    )
    with pytest.raises(OSError):
        with BackupArchiveWriter(
            final_path=target,
            metadata={"created_at": "2026-09-01T00:00:00Z", "backup_type": "manual"},
            signing_key="key",
            key_id="id",
            min_free_bytes=1,
        ) as writer:
            with writer.section("messages") as section:
                section.write({"msg_hash": "one"})
    assert validate_backup_archive(existing, signing_key="key").valid is True
    assert not target.exists()
    assert list(tmp_path.glob(".*.failed"))

    interrupted_target = tmp_path / "manual-backup-interrupted.cacb"
    writer = BackupArchiveWriter(
        final_path=interrupted_target,
        metadata={"created_at": "2026-09-01T00:00:00Z", "backup_type": "manual"},
        signing_key="key",
        key_id="id",
        min_free_bytes=0,
    )
    writer.abort(KeyboardInterrupt())
    assert not interrupted_target.exists()
    assert validate_backup_archive(existing, signing_key="key").valid is True


def test_v4_write_error_does_not_publish_or_damage_an_existing_backup(tmp_path, monkeypatch):
    existing = tmp_path / "auto-backup-existing.cacb"
    with BackupArchiveWriter(
        final_path=existing,
        metadata={"created_at": "2026-09-01T00:00:00Z", "backup_type": "auto"},
        signing_key="key",
        key_id="id",
        min_free_bytes=0,
    ) as existing_writer:
        with existing_writer.section("messages") as section:
            section.write({"msg_hash": "known-good"})
        existing_writer.set_count("messages", 1)
        existing_writer.finalize()

    target = tmp_path / "manual-backup-write-error.cacb"
    writer = BackupArchiveWriter(
        final_path=target,
        metadata={"created_at": "2026-09-01T00:00:00Z", "backup_type": "manual"},
        signing_key="key",
        key_id="id",
        min_free_bytes=0,
    )

    def fail_write(*_args, **_kwargs):
        raise OSError(errno.EIO, "injected archive write failure")

    monkeypatch.setattr(writer._tar, "addfile", fail_write)
    with pytest.raises(OSError, match="injected archive write failure"):
        with writer:
            with writer.section("messages") as section:
                section.write({"msg_hash": "never-published"})

    assert not target.exists()
    assert validate_backup_archive(existing, signing_key="key").valid is True


def test_v4_atomic_publish_refuses_to_overwrite_an_existing_name(tmp_path):
    target = tmp_path / "manual-backup-fixed-name.cacb"
    target.write_bytes(b"operator-owned-existing-backup")
    original = target.read_bytes()

    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        with BackupArchiveWriter(
            final_path=target,
            metadata={"created_at": "2026-09-01T00:00:00Z", "backup_type": "manual"},
            signing_key="key",
            key_id="id",
            min_free_bytes=0,
        ) as writer:
            with writer.section("messages") as section:
                section.write({"msg_hash": "must-not-replace"})
            writer.set_count("messages", 1)
            writer.finalize()

    assert target.read_bytes() == original
    assert list(tmp_path.glob(".*.failed"))


def test_backup_queue_allows_only_one_active_job(tmp_path):
    first = enqueue_backup_job(tmp_path, backup_type="manual", created_by="test", keep_latest=7)
    with pytest.raises(BackupAlreadyRunningError) as raised:
        enqueue_backup_job(tmp_path, backup_type="auto", created_by="scheduler", keep_latest=7)
    assert raised.value.job["job_id"] == first["job_id"]


def test_backup_execution_lock_allows_only_one_running_process(tmp_path):
    with backup_execution_lock(tmp_path):
        with pytest.raises(BackupAlreadyRunningError):
            with backup_execution_lock(tmp_path):
                pytest.fail("overlapping backup acquired the execution lock")


def test_worker_heartbeat_stays_fresh_during_a_long_job(tmp_path, monkeypatch):
    writes: list[float] = []
    monkeypatch.setattr("app.backup.worker.write_worker_heartbeat", lambda _root: writes.append(time.monotonic()))

    with _heartbeat_during_job(tmp_path, max_age_seconds=1, interval_seconds=0.01):
        time.sleep(0.045)

    assert len(writes) >= 4


@pytest.mark.asyncio
async def test_auto_and_manual_jobs_share_the_bounded_v4_worker_path(tmp_path):
    database_url, storage, engine, _sessions = await _source_database(tmp_path / "source")
    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    legacy = backup_root / "auto-backup-20250101T000000Z.json"
    legacy.write_bytes(b"preserved legacy backup")
    settings = Settings(
        database_url=database_url,
        storage_root=storage,
        backup_root=backup_root,
        public_storage_prefix="/static/storage",
        app_secret_key="worker-key",
        system_instance_id="worker-instance",
        backup_min_free_bytes=0,
        backup_worker_heartbeat_max_age_seconds=5,
    )

    for backup_type in ("auto", "manual"):
        queued = enqueue_backup_job(
            backup_root,
            backup_type=backup_type,
            created_by="pytest",
            keep_latest=1,
        )
        status = await run_one_job(settings)
        persisted = get_backup_job(backup_root, queued["job_id"])
        assert status is not None and status["state"] == "succeeded"
        assert persisted == status
        assert status["filename"].startswith(f"{backup_type}-backup-")
        assert status["peak_rss_mib"] > 0
        assert validate_backup_archive(backup_root / status["filename"], signing_key="worker-key").valid is True

    assert legacy.read_bytes() == b"preserved legacy backup"
    await engine.dispose()


def test_retention_only_deletes_excess_completed_auto_backups(tmp_path):
    completed = []
    for index in range(4):
        path = tmp_path / f"auto-backup-2026010{index + 1}T000000Z-0000000{index}.cacb"
        path.write_bytes(b"complete")
        os.utime(path, (index + 1, index + 1))
        completed.append(path)
    failed = tmp_path / ".auto-backup-failed.cacb.deadbeef.failed"
    failed.write_bytes(b"failed")
    manual = tmp_path / "manual-backup-20260105T000000Z-11111111.cacb"
    manual.write_bytes(b"manual")
    user_file = tmp_path / "my-backup.cacb"
    user_file.write_bytes(b"user")
    legacy = tmp_path / "auto-backup-20250101T000000Z.json"
    legacy.write_bytes(b"legacy")

    removed = apply_auto_backup_retention(tmp_path, 2)

    assert removed == completed[:2]
    assert all(not path.exists() for path in completed[:2])
    assert all(path.exists() for path in completed[2:])
    assert failed.exists() and manual.exists() and user_file.exists() and legacy.exists()


@pytest.mark.asyncio
async def test_sqlite_online_snapshot_excludes_writes_after_snapshot(tmp_path, monkeypatch):
    source_root = tmp_path / "source"
    database_url, storage, engine, _ = await _source_database(source_root)
    from app.backup import exporter

    original_copy = exporter._copy_sqlite_database

    def copy_then_write(source: Path, destination: Path) -> None:
        original_copy(source, destination)
        connection = sqlite3.connect(source)
        try:
            connection.execute(
                """
                INSERT INTO messages(
                    msg_hash, platform, room_id, message_type, sender_id,
                    raw_message, local_message, timestamp, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                ("after-snapshot", "qq", "room", "group", "user", "late", "late", 2),
            )
            connection.commit()
        finally:
            connection.close()

    monkeypatch.setattr(exporter, "_copy_sqlite_database", copy_then_write)
    backup = await create_full_backup_archive(
        database_url=database_url,
        storage_root=storage,
        backup_root=tmp_path / "backups",
        public_storage_prefix="/static/storage",
        signing_key="key",
        system_id="instance",
        backup_type="manual",
        created_by="pytest",
        min_free_bytes=0,
    )
    report = validate_backup_archive(backup.path, signing_key="key")
    connection = sqlite3.connect(source_root / "source.sqlite3")
    try:
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2
    finally:
        connection.close()
    assert report.counts["messages"] == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_signed_legacy_v3_streaming_conversion_restores_media(tmp_path):
    database_url, storage, engine, sessions = await _source_database(tmp_path / "source")
    async with sessions() as db:
        package = await BackupService.export_package(
            db,
            storage_root=storage,
            system_id="legacy-instance",
            signing_key="legacy-key",
        )
    legacy_path = tmp_path / "legacy-v3.json"
    legacy_path.write_text(json.dumps(package, ensure_ascii=False), encoding="utf-8")
    converted = convert_legacy_backup_to_v4(
        legacy_path,
        backup_root=tmp_path / "backups",
        signing_key="legacy-key",
        system_id="legacy-instance",
        require_legacy_signature=True,
        chunk_bytes=1024,
    )
    restored = await restore_backup_archive(
        converted.path,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'legacy-restore.sqlite3'}",
        storage_root=tmp_path / "legacy-media",
        public_storage_prefix="/static/storage",
        signing_key="legacy-key",
    )
    assert converted.manifest["legacy"]["checksum_valid"] is True
    assert converted.manifest["legacy"]["signature_valid"] is True
    assert restored.counts["messages"] == 1
    assert restored.media_files_created == 1
    assert (tmp_path / "legacy-media" / "asset.bin").read_bytes() == b"media-content"
    await engine.dispose()


def _memory_probe(media_bytes: int, messages: int) -> dict[str, float | int]:
    command = [
        sys.executable,
        str(Path(__file__).with_name("backup_memory_probe.py")),
        "--media-bytes",
        str(media_bytes),
        "--messages",
        str(messages),
    ]
    repository_root = Path(__file__).parents[1]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(repository_root)
    completed = subprocess.run(
        command,
        check=True,
        text=True,
        capture_output=True,
        timeout=180,
        cwd=repository_root,
        env=environment,
    )
    return json.loads(completed.stdout)


def test_large_backup_peak_memory_is_bounded_and_not_linear_with_media_bytes():
    small = _memory_probe(4 * 1024 * 1024, 25000)
    large = _memory_probe(256 * 1024 * 1024, 25000)
    assert large["peak_rss_mib"] < 512
    assert small["peak_rss_mib"] < 512
    assert large["peak_rss_mib"] - small["peak_rss_mib"] < 64


@pytest.mark.asyncio
async def test_postgresql_repeatable_read_keeps_cross_section_snapshot_consistent(tmp_path):
    database_url = os.environ.get("TEST_POSTGRES_URL")
    if not database_url:
        pytest.skip("TEST_POSTGRES_URL is not configured")
    from app.models import Base

    engine, sessions = create_async_engine_and_sessionmaker(database_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as db:
        db.add_all(
            [
                Message(
                    msg_hash="before",
                    platform="qq",
                    room_id="room",
                    message_type="group",
                    sender_id="user",
                    raw_message="before",
                    local_message="before",
                    timestamp=1,
                ),
                RobotMessage(robot_id="robot", msg_hash="before"),
            ]
        )
        await db.commit()

    async def mutate_after_messages(_section: str) -> None:
        async with sessions() as db:
            db.add_all(
                [
                    Message(
                        msg_hash="after",
                        platform="qq",
                        room_id="room",
                        message_type="group",
                        sender_id="user",
                        raw_message="after",
                        local_message="after",
                        timestamp=2,
                    ),
                    RobotMessage(robot_id="robot", msg_hash="after"),
                ]
            )
            await db.commit()

    backup = await create_full_backup_archive(
        database_url=database_url,
        storage_root=tmp_path / "pg-media",
        backup_root=tmp_path / "pg-backups",
        public_storage_prefix="/static/storage",
        signing_key="pg-key",
        system_id="pg-instance",
        backup_type="manual",
        created_by="pytest",
        min_free_bytes=0,
        progress_hook=mutate_after_messages,
    )
    report = validate_backup_archive(backup.path, signing_key="pg-key")
    assert report.counts["messages"] == 1
    assert report.counts["robot_messages"] == 1
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
    restored = await restore_backup_archive(
        backup.path,
        database_url=database_url,
        storage_root=tmp_path / "pg-restore-media",
        public_storage_prefix="/static/storage",
        signing_key="pg-key",
    )
    assert restored.counts["messages"] == 1
    assert restored.counts["robot_messages"] == 1
    async with sessions() as db:
        assert (await db.execute(select(func.count()).select_from(Message))).scalar_one() == 1
        assert (await db.execute(select(func.count()).select_from(RobotMessage))).scalar_one() == 1
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
    await engine.dispose()
