import asyncio
import os
import uuid

import pytest
from sqlalchemy import event, inspect, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.database import (
    LIGHTWEIGHT_MIGRATIONS,
    create_all_tables,
    create_async_engine_and_sessionmaker,
    ensure_schema_compatibility,
    rollback_lightweight_migration,
)
from app.models import SchemaMigration


async def test_completed_migrations_are_not_replayed_on_second_startup(tmp_path):
    engine, _sessions = create_async_engine_and_sessionmaker(
        f"sqlite+aiosqlite:///{tmp_path / 'startup.sqlite3'}"
    )
    try:
        await ensure_schema_compatibility(engine)
        statements = []

        def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
            statements.append(statement.strip().upper())

        event.listen(engine.sync_engine, "before_cursor_execute", capture)
        await create_all_tables(engine)
        await ensure_schema_compatibility(engine)
        event.remove(engine.sync_engine, "before_cursor_execute", capture)
        assert not any(sql.startswith(("CREATE INDEX", "CREATE UNIQUE INDEX", "DROP INDEX", "ALTER TABLE", "UPDATE ", "INSERT ", "DELETE ")) for sql in statements)
        async with engine.connect() as conn:
            versions = set((await conn.execute(select(SchemaMigration.version))).scalars())
        assert versions == set(LIGHTWEIGHT_MIGRATIONS)
    finally:
        await engine.dispose()


async def test_rolled_back_migration_is_applied_again_without_replaying_completed_versions(tmp_path):
    engine, _sessions = create_async_engine_and_sessionmaker(
        f"sqlite+aiosqlite:///{tmp_path / 'rollback-reapply.sqlite3'}"
    )
    version = "20260705_014_media_asset_local_path_index"
    try:
        await ensure_schema_compatibility(engine)
        await rollback_lightweight_migration(version, engine)
        async with engine.connect() as conn:
            assert version not in set((await conn.execute(select(SchemaMigration.version))).scalars())
            indexes = await conn.run_sync(lambda sync: {index["name"] for index in inspect(sync).get_indexes("media_assets")})
            assert "ix_media_assets_local_path" not in indexes
        await ensure_schema_compatibility(engine)
        async with engine.connect() as conn:
            assert set((await conn.execute(select(SchemaMigration.version))).scalars()) == set(LIGHTWEIGHT_MIGRATIONS)
            indexes = await conn.run_sync(lambda sync: {index["name"] for index in inspect(sync).get_indexes("media_assets")})
            assert "ix_media_assets_local_path" in indexes
    finally:
        await engine.dispose()


async def test_postgresql_second_startup_completes_while_backup_read_transaction_remains_open(tmp_path):
    database_url = os.environ.get("TEST_POSTGRES_URL")
    if not database_url:
        pytest.skip("TEST_POSTGRES_URL is not configured")
    from app.config import Settings
    from app.main import create_app

    # Never drop or migrate the configured database's public tables. Every
    # object in this integration test belongs to a fresh generated schema.
    schema = "ts324_startup_" + uuid.uuid4().hex
    admin_engine = create_async_engine(database_url, poolclass=NullPool)
    engine = None
    try:
        async with admin_engine.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_async_engine(
            database_url, poolclass=NullPool,
            connect_args={"server_settings": {"search_path": schema}},
        )
        await ensure_schema_compatibility(engine)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        settings = Settings(
            app_env="development", database_url=database_url,
            storage_root=tmp_path / "storage", backup_root=tmp_path / "backups",
        )
        application = create_app(settings=settings, engine=engine, sessionmaker=sessions)
        async with engine.connect() as reader:
            reader = await reader.execution_options(isolation_level="REPEATABLE READ")
            async with reader.begin():
                await reader.execute(text("SET TRANSACTION READ ONLY"))
                await reader.execute(text("SELECT count(*) FROM messages"))
                await reader.execute(text("SELECT count(*) FROM media_assets"))
                lifespan = application.router.lifespan_context(application)
                await asyncio.wait_for(lifespan.__aenter__(), timeout=5)
                try:
                    # Startup must finish before releasing this real backup-like
                    # AccessShare transaction, with no lock timeout or mock DDL.
                    assert reader.in_transaction()
                    assert (await reader.execute(text("SELECT 1"))).scalar_one() == 1
                finally:
                    await lifespan.__aexit__(None, None, None)
    finally:
        if engine is not None:
            await engine.dispose()
        async with admin_engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin_engine.dispose()
