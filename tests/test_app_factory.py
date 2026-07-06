from fastapi.testclient import TestClient
import json
from sqlalchemy import text
from sqlalchemy.pool import NullPool, StaticPool

from app.config import Settings
from app.database import LIGHTWEIGHT_MIGRATION_REGISTRY, LIGHTWEIGHT_MIGRATIONS, _table_columns, create_all_tables, create_async_engine_and_sessionmaker, ensure_schema_compatibility
from app.main import create_app


def test_lightweight_migration_registry_drives_status_order():
    versions = [migration.version for migration in LIGHTWEIGHT_MIGRATION_REGISTRY]

    assert versions == list(LIGHTWEIGHT_MIGRATIONS)
    assert len(versions) == len(set(versions))
    assert all(migration.description == LIGHTWEIGHT_MIGRATIONS[migration.version] for migration in LIGHTWEIGHT_MIGRATION_REGISTRY)


def test_create_async_engine_configures_sqlite_pool_classes(tmp_path):
    memory_engine, _memory_sessionmaker = create_async_engine_and_sessionmaker("sqlite+aiosqlite:///:memory:")
    file_engine, _file_sessionmaker = create_async_engine_and_sessionmaker(f"sqlite+aiosqlite:///{(tmp_path / 'audit.sqlite3').as_posix()}")

    assert isinstance(memory_engine.sync_engine.pool, StaticPool)
    assert isinstance(file_engine.sync_engine.pool, NullPool)


def test_create_async_engine_configures_postgres_pool():
    engine, _sessionmaker = create_async_engine_and_sessionmaker("postgresql+asyncpg://user:pass@localhost:5432/audit")
    pool = engine.sync_engine.pool

    assert pool.size() == 20
    assert pool._max_overflow == 10
    assert pool._timeout == 30
    assert pool._recycle == 3600
    assert pool._pre_ping is True


def test_table_columns_rejects_invalid_table_name():
    engine, _sessionmaker = create_async_engine_and_sessionmaker("sqlite+aiosqlite:///:memory:")

    async def inspect_columns():
        await create_all_tables(engine)
        async with engine.begin() as conn:
            message_columns = await _table_columns(conn, "messages")
            import pytest

            with pytest.raises(ValueError, match="Invalid table name"):
                await _table_columns(conn, "messages; drop table messages")
        await engine.dispose()
        return message_columns

    import anyio

    columns = anyio.run(inspect_columns)

    assert "msg_hash" in columns


def test_lightweight_migrations_upgrade_legacy_sqlite_schema(tmp_path):
    database_path = tmp_path / "legacy.sqlite3"
    engine, _sessionmaker = create_async_engine_and_sessionmaker(f"sqlite+aiosqlite:///{database_path.as_posix()}")

    async def arrange_legacy_schema():
        async with engine.begin() as conn:
            await conn.execute(text("create table adapters (id varchar(64) primary key, platform varchar(20) not null, config_json text, status varchar(20) not null, updated_at datetime not null)"))
            await conn.execute(text("create table messages (msg_hash varchar(64) primary key, platform varchar(20) not null, room_id varchar(64) not null, message_type varchar(20) not null, sender_id varchar(64) not null, nickname varchar(128), raw_message text not null, local_message text not null, timestamp integer not null, created_at datetime not null)"))

    async def inspect_after_upgrade():
        await ensure_schema_compatibility(engine)
        async with engine.connect() as conn:
            adapter_columns = [row[1] for row in (await conn.execute(text("pragma table_info(adapters)"))).fetchall()]
            message_columns = [row[1] for row in (await conn.execute(text("pragma table_info(messages)"))).fetchall()]
            migrations = [row[0] for row in (await conn.execute(text("select version from schema_migrations order by version"))).fetchall()]
        await engine.dispose()
        return adapter_columns, message_columns, migrations

    import anyio

    anyio.run(arrange_legacy_schema)
    adapter_columns, message_columns, migrations = anyio.run(inspect_after_upgrade)

    assert "current_robot_id" in adapter_columns
    assert "external_message_id" in message_columns
    assert migrations == list(LIGHTWEIGHT_MIGRATIONS)


def test_create_app_lifespan_initializes_storage_and_database(tmp_path):
    database_path = tmp_path / "audit.sqlite3"
    storage_root = tmp_path / "storage"
    backup_root = tmp_path / "backups"
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{database_path.as_posix()}",
        storage_root=storage_root,
        backup_root=backup_root,
        public_storage_prefix="/static/storage",
    )
    engine, sessionmaker = create_async_engine_and_sessionmaker(settings.database_url)
    app = create_app(settings=settings, engine=engine, sessionmaker=sessionmaker)

    with TestClient(app) as client:
        response = client.get("/health")
        migrations_response = client.get("/api/system/migrations")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "app": "chat-audit-core"}
    assert migrations_response.status_code == 200
    assert migrations_response.json() == [
        {
            "version": "20260705_001_adapter_current_robot_id",
            "description": "Add adapters.current_robot_id",
            "applied": True,
            "applied_at": migrations_response.json()[0]["applied_at"],
        },
        {
            "version": "20260705_002_message_external_message_id",
            "description": "Add messages.external_message_id",
            "applied": True,
            "applied_at": migrations_response.json()[1]["applied_at"],
        },
        {
            "version": "20260705_003_audit_logs",
            "description": "Create audit_logs table",
            "applied": True,
            "applied_at": migrations_response.json()[2]["applied_at"],
        },
        {
            "version": "20260705_004_schema_migrations",
            "description": "Create schema_migrations table",
            "applied": True,
            "applied_at": migrations_response.json()[3]["applied_at"],
        },
        {
            "version": "20260705_005_admin_tokens",
            "description": "Create admin_tokens table",
            "applied": True,
            "applied_at": migrations_response.json()[4]["applied_at"],
        },
        {
            "version": "20260705_006_system_settings",
            "description": "Create system_settings table",
            "applied": True,
            "applied_at": migrations_response.json()[5]["applied_at"],
        },
        {
            "version": "20260705_007_admin_users_sessions",
            "description": "Create admin_users and admin_sessions tables",
            "applied": True,
            "applied_at": migrations_response.json()[6]["applied_at"],
        },
        {
            "version": "20260705_008_capture_target_policies",
            "description": "Create capture_target_policies table",
            "applied": True,
            "applied_at": migrations_response.json()[7]["applied_at"],
        },
    ]
    assert storage_root.exists()
    assert backup_root.exists()

    async def inspect_tables():
        async with engine.connect() as conn:
                result = await conn.execute(
                    text("select name from sqlite_master where type='table' and name in ('messages', 'robot_messages', 'media_assets', 'adapters', 'bot_profiles', 'room_profiles', 'user_profiles', 'audit_logs', 'schema_migrations', 'admin_tokens', 'system_settings', 'admin_users', 'admin_sessions', 'capture_target_policies')")
                )
                return {row[0] for row in result.fetchall()}

    import anyio

    assert anyio.run(inspect_tables) == {"messages", "robot_messages", "media_assets", "adapters", "bot_profiles", "room_profiles", "user_profiles", "audit_logs", "schema_migrations", "admin_tokens", "system_settings", "admin_users", "admin_sessions", "capture_target_policies"}

    async def inspect_migrations():
        async with engine.connect() as conn:
            result = await conn.execute(text("select version from schema_migrations order by version"))
            return [row[0] for row in result.fetchall()]

    assert anyio.run(inspect_migrations) == [
        "20260705_001_adapter_current_robot_id",
        "20260705_002_message_external_message_id",
        "20260705_003_audit_logs",
        "20260705_004_schema_migrations",
        "20260705_005_admin_tokens",
            "20260705_006_system_settings",
            "20260705_007_admin_users_sessions",
            "20260705_008_capture_target_policies",
        ]



def test_create_app_lifespan_starts_auto_backup_scheduler(tmp_path, monkeypatch):
    database_path = tmp_path / "audit.sqlite3"
    storage_root = tmp_path / "storage"
    backup_root = tmp_path / "backups"
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{database_path.as_posix()}",
        storage_root=storage_root,
        backup_root=backup_root,
        public_storage_prefix="/static/storage",
        auto_backup_cron="15 3 * * *",
    )
    engine, sessionmaker = create_async_engine_and_sessionmaker(settings.database_url)
    calls = []

    class DummyTask:
        def cancel(self):
            calls.append("cancel")

    def fake_start_auto_backup_scheduler(*, settings, sessionmaker):
        calls.append((settings.auto_backup_cron, settings.backup_root))
        return DummyTask()

    monkeypatch.setattr("app.main.start_auto_backup_scheduler", fake_start_auto_backup_scheduler)
    app = create_app(settings=settings, engine=engine, sessionmaker=sessionmaker)

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert calls[0] == ("15 3 * * *", backup_root)
    assert calls[-1] == "cancel"



def test_create_app_rejects_default_secret_in_production(tmp_path):
    settings = Settings(
        app_env="production",
        app_secret_key="change-me-in-production",
        onebot_access_token="secret-token",
        admin_api_token="admin-token",
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'audit.sqlite3').as_posix()}",
        storage_root=tmp_path / "storage",
        backup_root=tmp_path / "backups",
    )
    engine, sessionmaker = create_async_engine_and_sessionmaker(settings.database_url)

    import pytest

    with pytest.raises(ValueError, match="APP_SECRET_KEY"):
        create_app(settings=settings, engine=engine, sessionmaker=sessionmaker)


def test_create_app_rejects_missing_onebot_token_in_production(tmp_path):
    settings = Settings(
        app_env="production",
        app_secret_key="strong-production-secret",
        onebot_access_token="",
        admin_api_token="admin-token",
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'audit.sqlite3').as_posix()}",
        storage_root=tmp_path / "storage",
        backup_root=tmp_path / "backups",
    )
    engine, sessionmaker = create_async_engine_and_sessionmaker(settings.database_url)

    import pytest

    with pytest.raises(ValueError, match="ONEBOT_ACCESS_TOKEN"):
        create_app(settings=settings, engine=engine, sessionmaker=sessionmaker)



def test_create_app_rejects_placeholder_secret_in_production(tmp_path):
    settings = Settings(
        app_env="production",
        app_secret_key="replace-with-a-long-random-secret",
        onebot_access_token="secret-token",
        admin_api_token="admin-token",
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'audit.sqlite3').as_posix()}",
        storage_root=tmp_path / "storage",
        backup_root=tmp_path / "backups",
    )
    engine, sessionmaker = create_async_engine_and_sessionmaker(settings.database_url)

    import pytest

    with pytest.raises(ValueError, match="APP_SECRET_KEY"):
        create_app(settings=settings, engine=engine, sessionmaker=sessionmaker)


def test_create_app_rejects_placeholder_onebot_token_in_production(tmp_path):
    settings = Settings(
        app_env="production",
        app_secret_key="strong-production-secret",
        onebot_access_token="replace-with-onebot-access-token",
        admin_api_token="admin-token",
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'audit.sqlite3').as_posix()}",
        storage_root=tmp_path / "storage",
        backup_root=tmp_path / "backups",
    )
    engine, sessionmaker = create_async_engine_and_sessionmaker(settings.database_url)

    import pytest

    with pytest.raises(ValueError, match="ONEBOT_ACCESS_TOKEN"):
        create_app(settings=settings, engine=engine, sessionmaker=sessionmaker)


def test_create_app_rejects_missing_admin_api_token_in_production(tmp_path):
    settings = Settings(
        app_env="production",
        app_secret_key="strong-production-secret",
        onebot_access_token="secret-token",
        admin_api_token="",
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'audit.sqlite3').as_posix()}",
        storage_root=tmp_path / "storage",
        backup_root=tmp_path / "backups",
    )
    engine, sessionmaker = create_async_engine_and_sessionmaker(settings.database_url)

    import pytest

    with pytest.raises(ValueError, match="ADMIN_API_TOKEN"):
        create_app(settings=settings, engine=engine, sessionmaker=sessionmaker)


def test_create_app_accepts_role_tokens_in_production(tmp_path):
    settings = Settings(
        app_env="production",
        app_secret_key="strong-production-secret",
        onebot_access_token="secret-token",
        admin_api_token="",
        admin_api_tokens=json.dumps([{"name": "ops", "role": "operator", "token": "operator-token"}]),
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'audit.sqlite3').as_posix()}",
        storage_root=tmp_path / "storage",
        backup_root=tmp_path / "backups",
    )
    engine, sessionmaker = create_async_engine_and_sessionmaker(settings.database_url)

    app = create_app(settings=settings, engine=engine, sessionmaker=sessionmaker)

    assert app.title == "chat-audit-core"


def test_create_app_rejects_invalid_role_tokens_in_production(tmp_path):
    settings = Settings(
        app_env="production",
        app_secret_key="strong-production-secret",
        onebot_access_token="secret-token",
        admin_api_token="",
        admin_api_tokens="not-json",
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'audit.sqlite3').as_posix()}",
        storage_root=tmp_path / "storage",
        backup_root=tmp_path / "backups",
    )
    engine, sessionmaker = create_async_engine_and_sessionmaker(settings.database_url)

    import pytest

    with pytest.raises(ValueError, match="ADMIN_API_TOKENS"):
        create_app(settings=settings, engine=engine, sessionmaker=sessionmaker)
