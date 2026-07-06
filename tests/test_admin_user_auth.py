import base64
import hashlib

from httpx import ASGITransport, AsyncClient
import pytest

from app.config import Settings, get_settings
from app.database import get_db_session
from app.main import app
from app.models import AdminUser
from app.services.admin_user_service import PASSWORD_HASH_ITERATIONS, AdminUserService


def _legacy_pbkdf2_hash(password: str) -> str:
    salt = b"legacy-pbkdf2-salt"
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_HASH_ITERATIONS)
    return "$".join(
        [
            "pbkdf2_sha256",
            str(PASSWORD_HASH_ITERATIONS),
            base64.urlsafe_b64encode(salt).decode("ascii"),
            base64.urlsafe_b64encode(digest).decode("ascii"),
        ]
    )


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
async def test_admin_password_hash_uses_bcrypt_for_new_users(db_session):
    user = await AdminUserService.create_user(
        db_session,
        username="bcrypt-user",
        password="strong-password-1",
        role="viewer",
    )

    assert user.password_hash.startswith("$2")
    assert AdminUserService.verify_password("strong-password-1", user.password_hash) is True
    assert AdminUserService.hash_password("strong-password-1") != AdminUserService.hash_password("strong-password-1")


@pytest.mark.asyncio
async def test_admin_login_upgrades_pbkdf2_password_hash(db_session):
    user = AdminUser(
        username="legacy-pbkdf2",
        role="viewer",
        password_hash=_legacy_pbkdf2_hash("strong-password-1"),
        status="active",
    )
    db_session.add(user)
    await db_session.commit()

    authenticated = await AdminUserService.authenticate(
        db_session,
        username="legacy-pbkdf2",
        password="strong-password-1",
    )

    assert authenticated is not None
    assert authenticated.password_hash.startswith("$2")
    assert AdminUserService.verify_password("strong-password-1", authenticated.password_hash) is True


@pytest.mark.asyncio
async def test_admin_login_upgrades_legacy_sha256_password_hash(db_session):
    user = AdminUser(
        username="legacy-sha256",
        role="viewer",
        password_hash=hashlib.sha256("strong-password-1".encode("utf-8")).hexdigest(),
        status="active",
    )
    db_session.add(user)
    await db_session.commit()

    authenticated = await AdminUserService.authenticate(
        db_session,
        username="legacy-sha256",
        password="strong-password-1",
    )

    assert authenticated is not None
    assert authenticated.password_hash.startswith("$2")
    assert AdminUserService.verify_password("strong-password-1", authenticated.password_hash) is True


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


@pytest.mark.asyncio
async def test_admin_user_password_reset_revokes_existing_sessions(db_session):
    settings = Settings(admin_api_token="bootstrap-admin")

    async def override_db_session():
        yield db_session

    app.dependency_overrides[get_db_session] = override_db_session
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            created = await client.post(
                "/api/admin/users",
                headers={"Authorization": "Bearer bootstrap-admin"},
                json={"username": "reset-user", "password": "old-password-1", "role": "viewer"},
            )
            login = await client.post("/api/auth/login", json={"username": "reset-user", "password": "old-password-1"})
            old_session_token = login.json()["token"]
            reset = await client.post(
                f"/api/admin/users/{created.json()['id']}/password",
                headers={"Authorization": "Bearer bootstrap-admin"},
                json={"password": "new-password-1"},
            )
            old_session_read = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {old_session_token}"})
            old_password_login = await client.post("/api/auth/login", json={"username": "reset-user", "password": "old-password-1"})
            new_password_login = await client.post("/api/auth/login", json={"username": "reset-user", "password": "new-password-1"})
    finally:
        app.dependency_overrides.clear()

    assert created.status_code == 201
    assert login.status_code == 200
    assert reset.status_code == 200
    assert old_session_read.status_code == 401
    assert old_password_login.status_code == 401
    assert new_password_login.status_code == 200


@pytest.mark.asyncio
async def test_admin_can_list_and_force_revoke_sessions(db_session):
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
                json={"username": "session-user", "password": "strong-password-1", "role": "viewer"},
            )
            login = await client.post("/api/auth/login", json={"username": "session-user", "password": "strong-password-1"})
            session_token = login.json()["token"]
            sessions = await client.get("/api/admin/sessions", headers={"Authorization": "Bearer bootstrap-admin"})
            session_id = sessions.json()[0]["id"]
            revoked = await client.delete(f"/api/admin/sessions/{session_id}", headers={"Authorization": "Bearer bootstrap-admin"})
            after_revoke = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {session_token}"})
    finally:
        app.dependency_overrides.clear()

    assert login.status_code == 200
    assert sessions.status_code == 200
    assert sessions.json()[0]["username"] == "session-user"
    assert sessions.json()[0]["status"] == "active"
    assert revoked.status_code == 200
    assert revoked.json()["status"] == "revoked"
    assert after_revoke.status_code == 401
