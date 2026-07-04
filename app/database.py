from collections.abc import AsyncIterator

from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.models import Adapter, Base, BotProfile, RobotMessage


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
        adapter_columns = await conn.run_sync(lambda sync_conn: {column["name"] for column in inspect(sync_conn).get_columns("adapters")})
        if "current_robot_id" not in adapter_columns:
            await conn.exec_driver_sql("ALTER TABLE adapters ADD COLUMN current_robot_id VARCHAR(64)")
        message_columns = await conn.run_sync(lambda sync_conn: {column["name"] for column in inspect(sync_conn).get_columns("messages")})
        if "external_message_id" not in message_columns:
            await conn.exec_driver_sql("ALTER TABLE messages ADD COLUMN external_message_id VARCHAR(64)")
            await conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_messages_external_message_id ON messages (external_message_id)")


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
