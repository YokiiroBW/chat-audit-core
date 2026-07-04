from httpx import ASGITransport, AsyncClient
import pytest

from app.config import Settings, get_settings
from app.database import get_db_session
from app.main import app


@pytest.mark.asyncio
async def test_adapter_crud_api_creates_updates_and_deletes_adapter(db_session):
    async def override_db_session():
        yield db_session

    app.dependency_overrides[get_db_session] = override_db_session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            create_response = await client.post(
                "/api/adapters",
                json={
                    "id": "robot-crud",
                    "platform": "qq",
                    "config_json": '{"reverse_ws":"/onebot/v11/ws"}',
                    "status": "gray",
                },
            )
            list_after_create = await client.get("/api/adapters")
            update_response = await client.patch(
                "/api/adapters/robot-crud",
                json={"status": "green", "config_json": '{"token":"configured"}'},
            )
            clear_config_response = await client.patch(
                "/api/adapters/robot-crud",
                json={"config_json": None, "status": "red"},
            )
            delete_response = await client.delete("/api/adapters/robot-crud")
            list_after_delete = await client.get("/api/adapters")
    finally:
        app.dependency_overrides.clear()

    assert create_response.status_code == 201
    assert create_response.json() == {
        "id": "robot-crud",
        "platform": "qq",
        "config_json": '{"reverse_ws":"/onebot/v11/ws"}',
        "status": "gray",
        "current_robot_id": None,
    }
    assert list_after_create.status_code == 200
    assert list_after_create.json() == [create_response.json()]
    assert update_response.status_code == 200
    assert update_response.json()["status"] == "green"
    assert update_response.json()["config_json"] == '{"token":"configured"}'
    assert clear_config_response.status_code == 200
    assert clear_config_response.json()["status"] == "red"
    assert clear_config_response.json()["config_json"] is None
    assert delete_response.status_code == 204
    assert list_after_delete.status_code == 200
    assert list_after_delete.json() == []


@pytest.mark.asyncio
async def test_adapter_crud_api_rejects_duplicate_and_missing_adapter(db_session):
    async def override_db_session():
        yield db_session

    app.dependency_overrides[get_db_session] = override_db_session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            first = await client.post("/api/adapters", json={"id": "robot-dup", "platform": "qq"})
            duplicate = await client.post("/api/adapters", json={"id": "robot-dup", "platform": "qq"})
            missing_patch = await client.patch("/api/adapters/not-found", json={"status": "red"})
            missing_delete = await client.delete("/api/adapters/not-found")
    finally:
        app.dependency_overrides.clear()

    assert first.status_code == 201
    assert duplicate.status_code == 409
    assert missing_patch.status_code == 404
    assert missing_delete.status_code == 404


@pytest.mark.asyncio
async def test_admin_api_token_is_required_when_configured(db_session):
    settings = Settings(admin_api_token="admin-secret")

    async def override_db_session():
        yield db_session

    app.dependency_overrides[get_db_session] = override_db_session
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            missing = await client.get("/api/adapters")
            invalid = await client.get("/api/adapters", headers={"Authorization": "Bearer wrong"})
            bearer = await client.get("/api/adapters", headers={"Authorization": "Bearer admin-secret"})
            custom_header = await client.get("/api/adapters", headers={"X-Admin-Token": "admin-secret"})
    finally:
        app.dependency_overrides.clear()

    assert missing.status_code == 401
    assert invalid.status_code == 401
    assert bearer.status_code == 200
    assert custom_header.status_code == 200
