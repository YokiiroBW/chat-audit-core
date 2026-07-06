from httpx import ASGITransport, AsyncClient
import pytest

from app.main import app


@pytest.mark.asyncio
async def test_openapi_includes_request_examples_and_endpoint_summaries():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/openapi.json")

    assert response.status_code == 200
    spec = response.json()

    message_schema = spec["components"]["schemas"]["MessageIngestRequest"]
    adapter_schema = spec["components"]["schemas"]["AdapterCreateRequest"]
    backup_schema = spec["components"]["schemas"]["BackupSettingsUpdateRequest"]
    assert message_schema["examples"][0]["robot_id"] == "1449801200"
    assert adapter_schema["examples"][0]["id"] == "napcat-26109"
    assert backup_schema["examples"][0]["cron"] == "0 3 * * *"

    assert spec["paths"]["/api/messages"]["post"]["summary"] == "Ingest a normalized message"
    assert "Capture policies" in spec["paths"]["/api/messages"]["post"]["description"]
    assert spec["paths"]["/api/export"]["get"]["summary"] == "Export chat backup package"
    assert "application/gzip" in spec["paths"]["/api/export"]["get"]["responses"]["200"]["content"]
    assert spec["paths"]["/api/import"]["post"]["summary"] == "Import a backup package"
