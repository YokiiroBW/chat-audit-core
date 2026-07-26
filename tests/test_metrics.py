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


@pytest.mark.asyncio
async def test_metrics_endpoint_requires_admin_auth_when_tokens_are_configured(db_session, tmp_path):
    # The export names actors, so an open /metrics published who operates the
    # deployment to anyone who could reach the port.
    from app.config import Settings, get_settings
    from app.database import get_db_session

    settings = Settings(admin_api_token="scrape-me", storage_root=tmp_path)

    async def override_db_session():
        yield db_session

    app.dependency_overrides[get_db_session] = override_db_session
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            anonymous = await client.get("/metrics")
            scraper = await client.get("/metrics", headers={"Authorization": "Bearer scrape-me"})
    finally:
        app.dependency_overrides.clear()

    assert anonymous.status_code == 401
    assert scraper.status_code == 200
    assert "chat_audit_" in scraper.text


@pytest.mark.asyncio
async def test_unmatched_paths_collapse_to_a_single_metric_label(db_session, tmp_path):
    # A raw path label gave every scanned URL its own time series and wrote
    # caller-chosen strings into the export.
    from app.config import Settings, get_settings
    from app.database import get_db_session

    settings = Settings(admin_api_token="scrape-me", storage_root=tmp_path)

    async def override_db_session():
        yield db_session

    app.dependency_overrides[get_db_session] = override_db_session
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await client.get("/no-such-path-aaa")
            await client.get("/no-such-path-bbb")
            metrics = await client.get("/metrics", headers={"Authorization": "Bearer scrape-me"})
    finally:
        app.dependency_overrides.clear()

    body = metrics.text
    assert "no-such-path-aaa" not in body
    assert "no-such-path-bbb" not in body
    assert 'endpoint="unmatched"' in body
