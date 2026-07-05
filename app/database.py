from collections.abc import AsyncIterator

from sqlalchemy import inspect, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.models import Adapter, Base, BotProfile, RobotMessage


LIGHTWEIGHT_MIGRATIONS = {
    "20260705_001_adapter_current_robot_id": "Add adapters.current_robot_id",
    "20260705_002_message_external_message_id": "Add messages.external_message_id",
    "20260705_003_audit_logs": "Create audit_logs table",
    "20260705_004_schema_migrations": "Create schema_migrations table",
}


def create_async_engine_and_sessionmaker(database_url: str | None = None) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    url = database_url or get_settings().database_url
    engine = create_async_engine(url, future=True)
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
        adapter_columns = await conn.run_sync(lambda sync_conn: {column["name"] for column in inspect(sync_conn).get_columns("adapters")})
        if "current_robot_id" not in adapter_columns:
            await conn.exec_driver_sql("ALTER TABLE adapters ADD COLUMN current_robot_id VARCHAR(64)")
        await _record_migration(conn, "20260705_001_adapter_current_robot_id")
        message_columns = await conn.run_sync(lambda sync_conn: {column["name"] for column in inspect(sync_conn).get_columns("messages")})
        if "external_message_id" not in message_columns:
            await conn.exec_driver_sql("ALTER TABLE messages ADD COLUMN external_message_id VARCHAR(64)")
            await conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_messages_external_message_id ON messages (external_message_id)")
        await _record_migration(conn, "20260705_002_message_external_message_id")
        await _record_migration(conn, "20260705_003_audit_logs")
        await _record_migration(conn, "20260705_004_schema_migrations")


async def _record_migration(conn, version: str) -> None:
    description = LIGHTWEIGHT_MIGRATIONS[version]
    dialect = conn.dialect.name
    if dialect == "sqlite":
        await conn.execute(
            text("INSERT OR IGNORE INTO schema_migrations (version, description, applied_at) VALUES (:version, :description, CURRENT_TIMESTAMP)"),
            {"version": version, "description": description},
        )
        return
    await conn.execute(
        text("INSERT INTO schema_migrations (version, description, applied_at) VALUES (:version, :description, CURRENT_TIMESTAMP) ON CONFLICT (version) DO NOTHING"),
        {"version": version, "description": description},
    )


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
