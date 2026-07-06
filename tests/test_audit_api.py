from httpx import ASGITransport, AsyncClient
import json
import pytest

from app.api import _RATE_LIMIT_BUCKETS
from app.config import Settings, get_settings
from app.database import get_db_session
from app.main import app
from app.services.adapter_service import AdapterService
from app.services.audit_log_service import REDACTED_VALUE, AuditLogService


@pytest.mark.asyncio
async def test_auth_failure_writes_audit_log(db_session):
    settings = Settings(admin_api_token="admin-secret")

    async def override_db_session():
        yield db_session

    app.dependency_overrides[get_db_session] = override_db_session
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            failed = await client.get("/api/adapters", headers={"Authorization": "Bearer wrong"})
            logs = await client.get("/api/audit/logs", headers={"Authorization": "Bearer admin-secret"})
    finally:
        app.dependency_overrides.clear()

    assert failed.status_code == 401
    assert logs.status_code == 200
    assert logs.json()[0]["action"] == "auth.failed"
    assert logs.json()[0]["status"] == "failed"
    assert logs.json()[0]["target"] == "/api/adapters"


@pytest.mark.asyncio
async def test_delete_adapter_writes_audit_log(db_session):
    await AdapterService.create_adapter(db_session, adapter_id="robot-audit-delete", platform="qq")

    async def override_db_session():
        yield db_session

    app.dependency_overrides[get_db_session] = override_db_session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            deleted = await client.delete("/api/adapters/robot-audit-delete")
            logs = await client.get("/api/audit/logs", params={"action": "adapter.delete"})
    finally:
        app.dependency_overrides.clear()

    assert deleted.status_code == 204
    assert logs.status_code == 200
    assert logs.json()[0]["action"] == "adapter.delete"
    assert logs.json()[0]["status"] == "success"
    assert logs.json()[0]["target"] == "robot-audit-delete"


@pytest.mark.asyncio
async def test_offline_repair_writes_audit_log(db_session, tmp_path):
    settings = Settings(storage_root=tmp_path / "storage", public_storage_prefix="/static/storage")

    async def override_db_session():
        yield db_session

    app.dependency_overrides[get_db_session] = override_db_session
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            repaired = await client.post("/api/offline/repair")
            logs = await client.get("/api/audit/logs", params={"action": "offline.repair"})
    finally:
        app.dependency_overrides.clear()

    assert repaired.status_code == 200
    assert logs.status_code == 200
    assert logs.json()[0]["action"] == "offline.repair"
    assert logs.json()[0]["status"] == "success"
    assert '"repaired_media_assets"' in logs.json()[0]["detail_json"]


@pytest.mark.asyncio
async def test_high_risk_rate_limit_blocks_repeated_operation(db_session):
    _RATE_LIMIT_BUCKETS.clear()
    settings = Settings(high_risk_rate_limit_per_minute=1)
    await AdapterService.create_adapter(db_session, adapter_id="robot-rate-limit", platform="qq")

    async def override_db_session():
        yield db_session

    app.dependency_overrides[get_db_session] = override_db_session
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            first = await client.delete("/api/adapters/robot-rate-limit")
            second = await client.delete("/api/adapters/not-found")
    finally:
        app.dependency_overrides.clear()
        _RATE_LIMIT_BUCKETS.clear()

    assert first.status_code == 204
    assert second.status_code == 429
    assert second.headers["Retry-After"] == "60"
    assert "adapter.delete" in second.json()["detail"]


@pytest.mark.asyncio
async def test_role_token_viewer_can_read_but_cannot_write(db_session, tmp_path):
    settings = Settings(
        admin_api_tokens=json.dumps([{"name": "readonly", "role": "viewer", "token": "viewer-token"}]),
        storage_root=tmp_path / "storage",
    )

    async def override_db_session():
        yield db_session

    app.dependency_overrides[get_db_session] = override_db_session
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            read_response = await client.get("/api/adapters", headers={"Authorization": "Bearer viewer-token"})
            write_response = await client.post("/api/offline/repair", headers={"Authorization": "Bearer viewer-token"})
            logs = await client.get("/api/audit/logs", headers={"Authorization": "Bearer viewer-token"}, params={"action": "auth.forbidden"})
    finally:
        app.dependency_overrides.clear()

    assert read_response.status_code == 200
    assert write_response.status_code == 403
    assert logs.status_code == 200
    assert logs.json()[0]["action"] == "auth.forbidden"
    assert logs.json()[0]["target"] == "/api/offline/repair"


@pytest.mark.asyncio
async def test_role_token_operator_can_repair_but_cannot_delete(db_session, tmp_path):
    settings = Settings(
        admin_api_tokens=json.dumps([{"name": "ops", "role": "operator", "token": "operator-token"}]),
        storage_root=tmp_path / "storage",
    )
    await AdapterService.create_adapter(db_session, adapter_id="robot-operator-delete", platform="qq")

    async def override_db_session():
        yield db_session

    app.dependency_overrides[get_db_session] = override_db_session
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            repair_response = await client.post("/api/offline/repair", headers={"Authorization": "Bearer operator-token"})
            delete_response = await client.delete("/api/adapters/robot-operator-delete", headers={"Authorization": "Bearer operator-token"})
    finally:
        app.dependency_overrides.clear()

    assert repair_response.status_code == 200
    assert delete_response.status_code == 403


@pytest.mark.asyncio
async def test_legacy_admin_api_token_keeps_admin_role(db_session):
    settings = Settings(admin_api_token="legacy-admin")
    await AdapterService.create_adapter(db_session, adapter_id="robot-legacy-admin", platform="qq")

    async def override_db_session():
        yield db_session

    app.dependency_overrides[get_db_session] = override_db_session
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.delete("/api/adapters/robot-legacy-admin", headers={"Authorization": "Bearer legacy-admin"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 204


@pytest.mark.asyncio
async def test_database_managed_admin_token_lifecycle(db_session):
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
                json={"name": "readonly-db", "role": "viewer"},
            )
            created_body = created.json()
            managed_token = created_body["token"]
            listed = await client.get("/api/admin/tokens", headers={"Authorization": "Bearer bootstrap-admin"})
            read_response = await client.get("/api/adapters", headers={"Authorization": f"Bearer {managed_token}"})
            write_response = await client.post("/api/offline/repair", headers={"Authorization": f"Bearer {managed_token}"})
            revoked = await client.delete(f"/api/admin/tokens/{created_body['id']}", headers={"Authorization": "Bearer bootstrap-admin"})
            after_revoke = await client.get("/api/adapters", headers={"Authorization": f"Bearer {managed_token}"})
    finally:
        app.dependency_overrides.clear()

    assert created.status_code == 201
    assert managed_token.startswith("cat_")
    assert created_body["token_prefix"] == managed_token[:12]
    assert created_body["role"] == "viewer"
    assert listed.status_code == 200
    assert listed.json()[0]["name"] == "readonly-db"
    assert listed.json()[0]["token"] is None
    assert read_response.status_code == 200
    assert write_response.status_code == 403
    assert revoked.status_code == 200
    assert revoked.json()["status"] == "revoked"
    assert after_revoke.status_code == 401


@pytest.mark.asyncio
async def test_audit_log_sanitization(db_session):
    log = await AuditLogService.record(
        db_session,
        action="sensitive.operation",
        status="success",
        detail={
            "username": "alice",
            "password": "plain-password",
            "access_token": "cat_secret_token",
            "nested": {
                "apiSecret": "secret-value",
                "items": [
                    {"authorization": "Bearer token"},
                    {"safe": "visible", "private_key": "key-value"},
                ],
            },
        },
    )
    detail = json.loads(log.detail_json)

    assert detail["username"] == "alice"
    assert detail["password"] == REDACTED_VALUE
    assert detail["access_token"] == REDACTED_VALUE
    assert detail["nested"]["apiSecret"] == REDACTED_VALUE
    assert detail["nested"]["items"][0]["authorization"] == REDACTED_VALUE
    assert detail["nested"]["items"][1]["safe"] == "visible"
    assert detail["nested"]["items"][1]["private_key"] == REDACTED_VALUE
    assert "plain-password" not in log.detail_json
    assert "cat_secret_token" not in log.detail_json
    assert "secret-value" not in log.detail_json
