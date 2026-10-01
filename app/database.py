from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Awaitable, Callable

from sqlalchemy import inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool, StaticPool

from app.config import get_settings
from app.models import Adapter, Base, BotProfile, RobotMessage


MigrationApply = Callable[[AsyncConnection], Awaitable[None]]


@dataclass(frozen=True)
class LightweightMigration:
    version: str
    description: str
    apply: MigrationApply
    rollback: MigrationApply | None = None


def _sql_literal(value: str) -> str:
    """Quote an identifier value for inline SQL, doubling embedded quotes."""
    return "'" + str(value).replace("'", "''") + "'"


async def _noop_migration(_conn) -> None:
    return None


async def _add_adapter_current_robot_id(conn) -> None:
    adapter_columns = await _table_columns(conn, "adapters")
    if "current_robot_id" not in adapter_columns:
        await conn.exec_driver_sql("ALTER TABLE adapters ADD COLUMN current_robot_id VARCHAR(64)")
    await conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_adapters_current_robot_id ON adapters (current_robot_id)")


async def _add_message_external_message_id(conn) -> None:
    message_columns = await _table_columns(conn, "messages")
    if "external_message_id" not in message_columns:
        await conn.exec_driver_sql("ALTER TABLE messages ADD COLUMN external_message_id VARCHAR(64)")
        await conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_messages_external_message_id ON messages (external_message_id)")


async def _add_unified_archive_schema(conn) -> None:
    def create_tables(sync_conn) -> None:
        for table_name in ("identity_aliases", "profile_change_records", "message_parts"):
            Base.metadata.tables[table_name].create(bind=sync_conn, checkfirst=True)
    await conn.run_sync(create_tables)
    for table_name, column_name, column_sql in (
        ("messages", "is_outgoing", "BOOLEAN"),
        ("media_assets", "content_sha256", "VARCHAR(64)"),
        ("message_media_references", "content_sha256", "VARCHAR(64)"),
        ("room_profiles", "avatar_file_hash", "VARCHAR(64)"),
        ("room_profiles", "avatar_source_url", "TEXT"),
        ("room_profiles", "avatar_status", "VARCHAR(20)"),
        ("user_profiles", "avatar_file_hash", "VARCHAR(64)"),
        ("user_profiles", "avatar_source_url", "TEXT"),
        ("user_profiles", "avatar_status", "VARCHAR(20)"),
    ):
        columns = await _table_columns(conn, table_name)
        if column_name not in columns:
            await conn.exec_driver_sql(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_sql}")
    await conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS idx_messages_is_outgoing ON messages (is_outgoing)")
    await conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS idx_media_assets_content_sha256 ON media_assets (content_sha256)")


async def _add_message_source_sequence(conn) -> None:
    message_columns = await _table_columns(conn, "messages")
    if "source_sequence" not in message_columns:
        await conn.exec_driver_sql("ALTER TABLE messages ADD COLUMN source_sequence INTEGER")
    await conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_room_timestamp_sequence ON messages (room_id, timestamp, source_sequence)"
    )


async def _add_admin_credential_expiry(conn) -> None:
    for table_name in ("admin_sessions", "admin_tokens"):
        columns = await _table_columns(conn, table_name)
        if "expires_at" not in columns:
            await conn.exec_driver_sql(f"ALTER TABLE {table_name} ADD COLUMN expires_at TIMESTAMP")


async def _rollback_admin_credential_expiry(conn) -> None:
    for table_name in ("admin_sessions", "admin_tokens"):
        await _drop_column_if_exists(conn, table_name, "expires_at")


async def _add_media_asset_local_path_index(conn) -> None:
    # Six services look assets up by local_path, including the per-message media
    # resolution on every read, and the column had no index.
    await conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_media_assets_local_path ON media_assets (local_path)")


async def _rollback_media_asset_local_path_index(conn) -> None:
    await conn.exec_driver_sql("DROP INDEX IF EXISTS ix_media_assets_local_path")


async def _drop_duplicate_column_indexes(conn) -> None:
    # These shadow indexes the models already declare on the same single column,
    # under a different prefix, so every write maintained two identical trees.
    # The model's ix_ index stays; only the idx_ twin goes.
    await conn.exec_driver_sql("DROP INDEX IF EXISTS idx_media_assets_content_sha256")
    await conn.exec_driver_sql("DROP INDEX IF EXISTS idx_messages_is_outgoing")


# Every column that names a media asset. A duplicate row cannot simply be
# deleted: whatever pointed at it has to be moved to the row that survives, or
# the message loses its media.
_MEDIA_ASSET_REFERENCES = (
    ("message_media_references", "asset_file_hash"),
    ("message_media_references", "thumbnail_file_hash"),
    ("room_profiles", "avatar_file_hash"),
    ("user_profiles", "avatar_file_hash"),
    ("profile_change_records", "old_avatar_file_hash"),
    ("profile_change_records", "new_avatar_file_hash"),
)


def merge_duplicate_media_assets_sync(execute) -> None:
    """Collapse assets sharing a content_sha256, then make the column unique.

    Rows predating content_sha256 carry NULL, which no database counts as a
    duplicate, so real collisions are rare -- but the unique index cannot be
    created while any exist. The surviving row is the oldest one, chosen by
    created_at then file_hash so the outcome does not depend on row order.

    Written against a plain ``execute`` callable so the lightweight registry and
    the alembic revision run the same code. They must not drift: this one
    rewrites and deletes rows.
    """
    duplicates = execute(
        "SELECT content_sha256 FROM media_assets "
        "WHERE content_sha256 IS NOT NULL "
        "GROUP BY content_sha256 HAVING COUNT(*) > 1"
    ).fetchall()
    for (content_sha256,) in duplicates:
        rows = execute(
            "SELECT file_hash FROM media_assets WHERE content_sha256 = "
            f"{_sql_literal(content_sha256)} ORDER BY created_at ASC, file_hash ASC"
        ).fetchall()
        keeper = rows[0][0]
        for (loser,) in rows[1:]:
            for table_name, column_name in _MEDIA_ASSET_REFERENCES:
                execute(
                    f"UPDATE {table_name} SET {column_name} = {_sql_literal(keeper)} "
                    f"WHERE {column_name} = {_sql_literal(loser)}"
                )
            execute(f"DELETE FROM media_assets WHERE file_hash = {_sql_literal(loser)}")
    execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_media_assets_content_sha256 ON media_assets (content_sha256)")
    # NOT NULL with a default on the model, so a NULL here is a row that predates
    # the column. Left as NULL the avatar is neither cached nor pending, and
    # nothing ever picks it up again.
    for table_name in ("room_profiles", "user_profiles"):
        execute(f"UPDATE {table_name} SET avatar_status = 'unknown' WHERE avatar_status IS NULL")


async def _merge_duplicate_media_assets(conn) -> None:
    await conn.run_sync(lambda sync_conn: merge_duplicate_media_assets_sync(sync_conn.exec_driver_sql))


async def _rollback_merge_duplicate_media_assets(conn) -> None:
    # Merged rows cannot be brought back; only the constraint is reversible.
    await conn.exec_driver_sql("DROP INDEX IF EXISTS ix_media_assets_content_sha256")


async def _add_performance_indexes(conn) -> None:
    await conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS idx_room_timestamp ON messages (room_id, timestamp)")
    await conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS idx_platform_room_timestamp ON messages (platform, room_id, timestamp)")
    await conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS idx_sender_timestamp ON messages (sender_id, timestamp)")
    await conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS idx_message_type_timestamp ON messages (message_type, timestamp)")
    await conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS idx_robot_message_robot_msg_hash ON robot_messages (robot_id, msg_hash)")


_QQNT_IMPORT_TABLES = (
    "import_sources",
    "import_batches",
    "import_batch_chunks",
    "message_source_records",
    "message_media_references",
    "identity_aliases",
    "profile_change_records",
    "message_parts",
)


async def _create_qqnt_import_tables(conn) -> None:
    def create_tables(sync_conn) -> None:
        for table_name in _QQNT_IMPORT_TABLES:
            Base.metadata.tables[table_name].create(bind=sync_conn, checkfirst=True)

    await conn.run_sync(create_tables)


async def _rollback_qqnt_import_tables(conn) -> None:
    def drop_tables(sync_conn) -> None:
        for table_name in reversed(_QQNT_IMPORT_TABLES):
            Base.metadata.tables[table_name].drop(bind=sync_conn, checkfirst=True)

    await conn.run_sync(drop_tables)


async def _drop_column_if_exists(conn, table_name: str, column_name: str) -> None:
    table_columns = await _table_columns(conn, table_name)
    if column_name in table_columns:
        await conn.exec_driver_sql(f"ALTER TABLE {table_name} DROP COLUMN {column_name}")


async def _rollback_adapter_current_robot_id(conn) -> None:
    await conn.exec_driver_sql("DROP INDEX IF EXISTS ix_adapters_current_robot_id")
    await _drop_column_if_exists(conn, "adapters", "current_robot_id")


async def _rollback_message_external_message_id(conn) -> None:
    await conn.exec_driver_sql("DROP INDEX IF EXISTS ix_messages_external_message_id")
    await _drop_column_if_exists(conn, "messages", "external_message_id")


async def _rollback_performance_indexes(conn) -> None:
    await conn.exec_driver_sql("DROP INDEX IF EXISTS idx_robot_message_robot_msg_hash")
    await conn.exec_driver_sql("DROP INDEX IF EXISTS idx_message_type_timestamp")
    await conn.exec_driver_sql("DROP INDEX IF EXISTS idx_sender_timestamp")
    await conn.exec_driver_sql("DROP INDEX IF EXISTS idx_platform_room_timestamp")


LIGHTWEIGHT_MIGRATION_REGISTRY = (
    LightweightMigration("20260705_001_adapter_current_robot_id", "Add adapters.current_robot_id", _add_adapter_current_robot_id, _rollback_adapter_current_robot_id),
    LightweightMigration("20260705_002_message_external_message_id", "Add messages.external_message_id", _add_message_external_message_id, _rollback_message_external_message_id),
    LightweightMigration("20260705_003_audit_logs", "Create audit_logs table", _noop_migration),
    LightweightMigration("20260705_004_schema_migrations", "Create schema_migrations table", _noop_migration),
    LightweightMigration("20260705_005_admin_tokens", "Create admin_tokens table", _noop_migration),
    LightweightMigration("20260705_006_system_settings", "Create system_settings table", _noop_migration),
    LightweightMigration("20260705_007_admin_users_sessions", "Create admin_users and admin_sessions tables", _noop_migration),
    LightweightMigration("20260705_008_capture_target_policies", "Create capture_target_policies table", _noop_migration),
    LightweightMigration("20260705_009_performance_indexes", "Create performance indexes", _add_performance_indexes, _rollback_performance_indexes),
    LightweightMigration("20260705_010_qqnt_import_models", "Create QQNT import and media reference tables", _create_qqnt_import_tables, _rollback_qqnt_import_tables),
    LightweightMigration("20260705_011_message_source_sequence", "Add source message sequence ordering", _add_message_source_sequence, None),
    LightweightMigration("20260705_012_unified_archive_schema", "Add unified identities, message parts, profile and media metadata", _add_unified_archive_schema, None),
    LightweightMigration("20260705_013_admin_credential_expiry", "Add admin session and token expiry", _add_admin_credential_expiry, _rollback_admin_credential_expiry),
    LightweightMigration("20260705_014_media_asset_local_path_index", "Index media assets by local path", _add_media_asset_local_path_index, _rollback_media_asset_local_path_index),
    LightweightMigration("20260705_015_drop_duplicate_column_indexes", "Drop duplicate single-column indexes", _drop_duplicate_column_indexes, None),
    LightweightMigration("20260705_016_media_asset_content_uniqueness", "Merge duplicate media assets and backfill avatar status", _merge_duplicate_media_assets, _rollback_merge_duplicate_media_assets),
)

LIGHTWEIGHT_MIGRATIONS = {migration.version: migration.description for migration in LIGHTWEIGHT_MIGRATION_REGISTRY}
VALID_TABLE_NAMES = frozenset(Base.metadata.tables)


def create_async_engine_and_sessionmaker(database_url: str | None = None) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    settings = get_settings()
    url = database_url or settings.database_url
    parsed_url = make_url(url)
    engine_kwargs: dict[str, object] = {"future": True}
    if parsed_url.get_backend_name() == "sqlite":
        is_memory_database = parsed_url.database in {None, "", ":memory:"}
        engine_kwargs["poolclass"] = StaticPool if is_memory_database else NullPool
        engine_kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    else:
        engine_kwargs.update(
            {
                "pool_size": settings.database_pool_size,
                "max_overflow": settings.database_max_overflow,
                "pool_timeout": settings.database_pool_timeout_seconds,
                "pool_recycle": settings.database_pool_recycle_seconds,
                "pool_pre_ping": True,
            }
        )
    engine = create_async_engine(url, **engine_kwargs)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    return engine, sessionmaker


engine, AsyncSessionLocal = create_async_engine_and_sessionmaker()


async def create_all_tables(target_engine: AsyncEngine | None = None) -> None:
    active_engine = target_engine or engine
    async with active_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def ensure_schema_compatibility(target_engine: AsyncEngine | None = None) -> None:
    active_engine = target_engine or engine
    async with active_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        for migration in LIGHTWEIGHT_MIGRATION_REGISTRY:
            await migration.apply(conn)
            await _record_migration(conn, migration)


async def _table_columns(conn, table_name: str) -> set[str]:
    if table_name not in VALID_TABLE_NAMES:
        raise ValueError(f"Invalid table name: {table_name}")
    return await conn.run_sync(lambda sync_conn: {column["name"] for column in inspect(sync_conn).get_columns(table_name)})


async def _record_migration(conn, migration: LightweightMigration) -> None:
    dialect = conn.dialect.name
    if dialect == "sqlite":
        await conn.execute(
            text("INSERT OR IGNORE INTO schema_migrations (version, description, applied_at) VALUES (:version, :description, CURRENT_TIMESTAMP)"),
            {"version": migration.version, "description": migration.description},
        )
        return
    await conn.execute(
        text("INSERT INTO schema_migrations (version, description, applied_at) VALUES (:version, :description, CURRENT_TIMESTAMP) ON CONFLICT (version) DO NOTHING"),
        {"version": migration.version, "description": migration.description},
    )


async def rollback_migration(conn: AsyncConnection, migration: LightweightMigration) -> None:
    if migration.rollback is None:
        raise ValueError(f"Migration {migration.version} does not support rollback")
    await migration.rollback(conn)
    await conn.execute(text("DELETE FROM schema_migrations WHERE version = :version"), {"version": migration.version})


async def rollback_lightweight_migration(version: str, target_engine: AsyncEngine | None = None) -> None:
    migration = next((item for item in LIGHTWEIGHT_MIGRATION_REGISTRY if item.version == version), None)
    if migration is None:
        raise ValueError(f"Unknown lightweight migration: {version}")
    active_engine = target_engine or engine
    async with active_engine.begin() as conn:
        await rollback_migration(conn, migration)


async def backfill_bot_profiles(sessionmaker: async_sessionmaker[AsyncSession] | None = None) -> None:
    active_sessionmaker = sessionmaker or AsyncSessionLocal
    async with active_sessionmaker() as session:
        existing_result = await session.execute(select(BotProfile.id))
        existing_ids = set(existing_result.scalars().all())

        robot_result = await session.execute(select(RobotMessage.robot_id).distinct())
        robot_ids = set(robot_result.scalars().all())

        adapter_result = await session.execute(select(Adapter))
        for adapter in adapter_result.scalars().all():
            adapter_id_looks_like_legacy_robot = adapter.id in robot_ids or adapter.id.isdigit()
            if adapter_id_looks_like_legacy_robot and adapter.id not in existing_ids:
                session.add(
                    BotProfile(
                        id=adapter.id,
                        platform=adapter.platform,
                        status=adapter.status,
                        source_adapter_id=adapter.id,
                        first_seen_at=adapter.updated_at,
                        last_seen_at=adapter.updated_at,
                    )
                )
                existing_ids.add(adapter.id)
            if adapter_id_looks_like_legacy_robot and adapter.current_robot_id is None:
                adapter.current_robot_id = adapter.id

        for robot_id in robot_ids:
            if robot_id not in existing_ids:
                session.add(BotProfile(id=robot_id, platform="qq"))
                existing_ids.add(robot_id)

        await session.commit()


async def get_db_session() -> AsyncIterator[AsyncSession]:
    async with AsyncSessionLocal() as session:
        yield session
