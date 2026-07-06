from httpx import ASGITransport, AsyncClient
import pytest

from app.config import Settings
from app.database import create_async_engine_and_sessionmaker
from app.main import app, create_app


@pytest.mark.asyncio
async def test_health_endpoint_reports_ok():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["checks"]["database"] == "ok"
    assert response.json()["checks"]["storage"] == "ok"
    assert response.json()["checks"]["backup"] == "ok"


class BrokenSessionmaker:
    def __call__(self):
        return self

    async def __aenter__(self):
        raise RuntimeError("database unavailable")

    async def __aexit__(self, exc_type, exc, tb):
        return False


@pytest.mark.asyncio
async def test_health_check_dependencies_report_database_failure(tmp_path):
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'health.sqlite3').as_posix()}",
        storage_root=tmp_path / "storage",
        backup_root=tmp_path / "backups",
    )
    engine, _sessionmaker = create_async_engine_and_sessionmaker(settings.database_url)
    test_app = create_app(settings=settings, engine=engine, sessionmaker=BrokenSessionmaker())

    async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 503
    assert response.json()["status"] == "degraded"
    assert response.json()["checks"]["database"] == "error"
    assert response.json()["checks"]["storage"] == "ok"


@pytest.mark.asyncio
async def test_health_check_dependencies_report_ffmpeg_failure(tmp_path):
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'health.sqlite3').as_posix()}",
        storage_root=tmp_path / "storage",
        backup_root=tmp_path / "backups",
        media_transcode_enabled=True,
        ffmpeg_bin="missing-ffmpeg-for-health",
    )
    engine, sessionmaker = create_async_engine_and_sessionmaker(settings.database_url)
    test_app = create_app(settings=settings, engine=engine, sessionmaker=sessionmaker)

    async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 503
    assert response.json()["status"] == "degraded"
    assert response.json()["checks"]["database"] == "ok"
    assert response.json()["checks"]["ffmpeg"] == "error"
