from httpx import ASGITransport, AsyncClient
import pytest

from app.main import app
from app.metrics import metrics_registry


@pytest.mark.asyncio
async def test_metrics_endpoint_exports_prometheus_text():
    metrics_registry.record_media_download(media_type="image", status="success")
    metrics_registry.record_rate_limit_exceeded(action="adapter.delete", actor="operator")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        health = await client.get("/health")
        metrics = await client.get("/metrics")

    assert health.status_code == 200
    assert metrics.status_code == 200
    assert "text/plain" in metrics.headers["content-type"]
    body = metrics.text
    assert "chat_audit_http_requests_total" in body
    assert 'endpoint="/health"' in body
    assert "chat_audit_http_request_duration_seconds_count" in body
    assert "chat_audit_websocket_connections" in body
    assert 'chat_audit_media_download_total{media_type="image",status="success"}' in body
    assert 'chat_audit_rate_limit_exceeded_total{action="adapter.delete",actor="operator"}' in body
