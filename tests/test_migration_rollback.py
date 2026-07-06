from sqlalchemy import inspect, text

from app.database import (
    LIGHTWEIGHT_MIGRATION_REGISTRY,
    create_all_tables,
    create_async_engine_and_sessionmaker,
    ensure_schema_compatibility,
    rollback_lightweight_migration,
)


async def _columns(engine, table_name: str) -> set[str]:
    async with engine.connect() as conn:
        return await conn.run_sync(lambda sync_conn: {column["name"] for column in inspect(sync_conn).get_columns(table_name)})


async def _migration_versions(engine) -> set[str]:
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT version FROM schema_migrations"))
        return {row[0] for row in result.fetchall()}


async def test_lightweight_migration_can_rollback_adapter_current_robot_id(tmp_path):
    engine, _sessionmaker = create_async_engine_and_sessionmaker(f"sqlite+aiosqlite:///{(tmp_path / 'rollback.sqlite3').as_posix()}")
    await create_all_tables(engine)
    await ensure_schema_compatibility(engine)

    assert "current_robot_id" in await _columns(engine, "adapters")
    assert "20260705_001_adapter_current_robot_id" in await _migration_versions(engine)

    await rollback_lightweight_migration("20260705_001_adapter_current_robot_id", engine)

    assert "current_robot_id" not in await _columns(engine, "adapters")
    assert "20260705_001_adapter_current_robot_id" not in await _migration_versions(engine)


async def test_lightweight_migration_can_rollback_message_external_message_id(tmp_path):
    engine, _sessionmaker = create_async_engine_and_sessionmaker(f"sqlite+aiosqlite:///{(tmp_path / 'rollback.sqlite3').as_posix()}")
    await create_all_tables(engine)
    await ensure_schema_compatibility(engine)

    assert "external_message_id" in await _columns(engine, "messages")
    assert "20260705_002_message_external_message_id" in await _migration_versions(engine)

    await rollback_lightweight_migration("20260705_002_message_external_message_id", engine)

    assert "external_message_id" not in await _columns(engine, "messages")
    assert "20260705_002_message_external_message_id" not in await _migration_versions(engine)


async def test_lightweight_migration_without_rollback_is_rejected(tmp_path):
    engine, _sessionmaker = create_async_engine_and_sessionmaker(f"sqlite+aiosqlite:///{(tmp_path / 'rollback.sqlite3').as_posix()}")
    await create_all_tables(engine)
    await ensure_schema_compatibility(engine)

    migration_without_rollback = next(item for item in LIGHTWEIGHT_MIGRATION_REGISTRY if item.rollback is None)

    try:
        await rollback_lightweight_migration(migration_without_rollback.version, engine)
    except ValueError as exc:
        assert "does not support rollback" in str(exc)
    else:
        raise AssertionError("rollback should fail for migrations without rollback support")
