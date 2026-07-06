from fastapi.testclient import TestClient

from app.config import Settings
from app.database import create_async_engine_and_sessionmaker
from app.main import CSRF_COOKIE_NAME, create_app


def test_browser_unsafe_request_requires_csrf_token(tmp_path):
    settings = Settings(
        admin_api_token="admin-secret",
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'audit.sqlite3').as_posix()}",
        storage_root=tmp_path / "storage",
        backup_root=tmp_path / "backups",
    )
    engine, sessionmaker = create_async_engine_and_sessionmaker(settings.database_url)
    app = create_app(settings=settings, engine=engine, sessionmaker=sessionmaker)

    with TestClient(app) as client:
        health = client.get("/health")
        csrf_token = health.cookies.get(CSRF_COOKIE_NAME)

        valid = client.delete(
            "/api/adapters/not-found",
            headers={
                "Authorization": "Bearer admin-secret",
                "Sec-Fetch-Site": "same-origin",
                "X-CSRF-Token": csrf_token,
            },
        )
        missing = client.delete(
            "/api/adapters/not-found",
            headers={
                "Authorization": "Bearer admin-secret",
                "Sec-Fetch-Site": "same-origin",
            },
        )

    assert csrf_token
    assert missing.status_code == 403
    assert missing.json()["detail"] == "CSRF token missing or invalid"
    assert valid.status_code != 403


def test_non_browser_api_client_keeps_bearer_token_compatibility(tmp_path):
    settings = Settings(
        admin_api_token="admin-secret",
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'audit.sqlite3').as_posix()}",
        storage_root=tmp_path / "storage",
        backup_root=tmp_path / "backups",
    )
    engine, sessionmaker = create_async_engine_and_sessionmaker(settings.database_url)
    app = create_app(settings=settings, engine=engine, sessionmaker=sessionmaker)

    with TestClient(app) as client:
        response = client.delete("/api/adapters/not-found", headers={"Authorization": "Bearer admin-secret"})

    assert response.status_code != 403
