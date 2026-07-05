from httpx import ASGITransport, AsyncClient
import pytest

from app.config import Settings, get_settings
from app.database import get_db_session
from app.main import app


@pytest.mark.asyncio
async def test_database_user_login_logout_and_role_authorization(db_session, tmp_path):
    settings = Settings(admin_api_token="bootstrap-admin", storage_root=tmp_path / "storage")

    async def override_db_session():
        yield db_session

    app.dependency_overrides[get_db_session] = override_db_session
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            created = await client.post(
                "/api/admin/users",
                headers={"Authorization": "Bearer bootstrap-admin"},
                json={"username": "OpsUser", "password": "strong-password-1", "role": "operator", "display_name": "Ops User"},
            )
            login = await client.post("/api/auth/login", json={"username": "opsuser", "password": "strong-password-1"})
            session_token = login.json()["token"]
            me = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {session_token}"})
            repair = await client.post("/api/offline/repair", headers={"Authorization": f"Bearer {session_token}"})
            delete = await client.delete("/api/adapters/not-allowed", headers={"Authorization": f"Bearer {session_token}"})
            logout = await client.post("/api/auth/logout", headers={"Authorization": f"Bearer {session_token}"})
            after_logout = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {session_token}"})
            revoked = await client.delete(f"/api/admin/users/{created.json()['id']}", headers={"Authorization": "Bearer bootstrap-admin"})
            after_revoke_login = await client.post("/api/auth/login", json={"username": "opsuser", "password": "strong-password-1"})
            logs = await client.get("/api/audit/logs", headers={"Authorization": "Bearer bootstrap-admin"}, params={"action": "auth.login"})
    finally:
        app.dependency_overrides.clear()

    assert created.status_code == 201
    assert created.json()["username"] == "opsuser"
    assert created.json()["role"] == "operator"
    assert login.status_code == 200
    assert session_token.startswith("cas_")
    assert me.status_code == 200
    assert me.json()["actor"] == "db-user:opsuser"
    assert me.json()["role"] == "operator"
    assert repair.status_code == 200
    assert delete.status_code == 403
    assert logout.status_code == 204
    assert after_logout.status_code == 401
    assert revoked.status_code == 200
    assert revoked.json()["status"] == "revoked"
    assert after_revoke_login.status_code == 401
    assert logs.status_code == 200
    assert {record["status"] for record in logs.json()} >= {"success", "failed"}


@pytest.mark.asyncio
async def test_database_user_login_rejects_bad_password(db_session):
    settings = Settings(admin_api_token="bootstrap-admin")

    async def override_db_session():
        yield db_session

    app.dependency_overrides[get_db_session] = override_db_session
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await client.post(
                "/api/admin/users",
                headers={"Authorization": "Bearer bootstrap-admin"},
                json={"username": "viewer", "password": "strong-password-1", "role": "viewer"},
            )
            login = await client.post("/api/auth/login", json={"username": "viewer", "password": "wrong-password"})
    finally:
        app.dependency_overrides.clear()

    assert login.status_code == 401


@pytest.mark.asyncio
async def test_database_managed_admin_token_rotation(db_session):
    settings = Settings(admin_api_token="bootstrap-admin")

    async def override_db_session():
        yield db_session

    app.dependency_overrides[get_db_session] = override_db_session
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            created = await client.post(
                "/api/admin/tokens",
                headers={"Authorization": "Bearer bootstrap-admin"},
                json={"name": "rotating", "role": "viewer"},
            )
            old_token = created.json()["token"]
            rotated = await client.post(
                f"/api/admin/tokens/{created.json()['id']}/rotate",
                headers={"Authorization": "Bearer bootstrap-admin"},
            )
            new_token = rotated.json()["token"]
            old_read = await client.get("/api/adapters", headers={"Authorization": f"Bearer {old_token}"})
            new_read = await client.get("/api/adapters", headers={"Authorization": f"Bearer {new_token}"})
    finally:
        app.dependency_overrides.clear()

    assert created.status_code == 201
    assert rotated.status_code == 200
    assert new_token.startswith("cat_")
    assert new_token != old_token
    assert rotated.json()["token_prefix"] == new_token[:12]
    assert old_read.status_code == 401
    assert new_read.status_code == 200
