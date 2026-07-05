from httpx import ASGITransport, AsyncClient
import pytest

from app.api import _RATE_LIMIT_BUCKETS
from app.config import Settings, get_settings
from app.database import get_db_session
from app.main import app
from app.services.adapter_service import AdapterService


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
