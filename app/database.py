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


async def _add_performance_indexes(conn) -> None:
    await conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS idx_room_timestamp ON messages (room_id, timestamp)")
    await conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS idx_platform_room_timestamp ON messages (platform, room_id, timestamp)")
    await conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS idx_sender_timestamp ON messages (sender_id, timestamp)")
    await conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS idx_message_type_timestamp ON messages (message_type, timestamp)")
    await conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS idx_robot_message_robot_msg_hash ON robot_messages (robot_id, msg_hash)")


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
        engine_kwargs["connect_args"] = {"check_same_thread": False}
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
