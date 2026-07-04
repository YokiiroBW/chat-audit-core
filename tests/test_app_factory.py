from fastapi.testclient import TestClient
from sqlalchemy import text

from app.config import Settings
from app.database import create_async_engine_and_sessionmaker
from app.main import create_app


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

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "app": "chat-audit-core"}
    assert storage_root.exists()
    assert backup_root.exists()

    async def inspect_tables():
        async with engine.connect() as conn:
            result = await conn.execute(
                text("select name from sqlite_master where type='table' and name in ('messages', 'robot_messages', 'media_assets', 'adapters', 'bot_profiles', 'room_profiles')")
            )
            return {row[0] for row in result.fetchall()}

    import anyio

    assert anyio.run(inspect_tables) == {"messages", "robot_messages", "media_assets", "adapters", "bot_profiles", "room_profiles"}



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
