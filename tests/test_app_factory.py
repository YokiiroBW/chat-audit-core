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
                text("select name from sqlite_master where type='table' and name in ('messages', 'robot_messages', 'media_assets', 'adapters')")
            )
            return {row[0] for row in result.fetchall()}

    import anyio

    assert anyio.run(inspect_tables) == {"messages", "robot_messages", "media_assets", "adapters"}
