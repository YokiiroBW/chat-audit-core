from httpx import ASGITransport, AsyncClient
import pytest

from app.main import app


@pytest.mark.asyncio
async def test_web_console_index_serves_three_column_dashboard():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    html = response.text
    assert "社交资产多租户审计控制台" in html
    assert "AUDIT V4" in html
    assert "/api/adapters" in html
    assert "/api/rooms" in html
    assert "/api/messages" in html
    assert "/api/search" in html
    assert "performSearch" in html
    assert "searchResults" in html
    assert "selectSearchResult" in html
    assert "搜索结果" in html
    assert "before_timestamp" in html
    assert "static/storage" in html
