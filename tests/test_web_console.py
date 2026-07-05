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
    assert "unpkg.com" not in html
    assert "cdn.tailwindcss.com" not in html
    assert "ElementPlus" not in html
    assert "Vue" not in html
    assert "/api/adapters" in html
    assert "/api/bots" in html
    assert "/api/dashboard" in html
    assert "dashboardSummary" in html
    assert "renderDashboard" in html
    assert "/api/rooms" in html
    assert "/api/messages" in html
    assert "/api/search" in html
    assert "performSearch" in html
    assert "searchResults" in html
    assert "selectSearchResult" in html
    assert "搜索结果" in html
    assert "before_timestamp" in html
    assert "static/storage" in html
    assert "parseCQSegment" in html
    assert "renderCQJsonCard" in html
    assert "打开卡片网页" in html
    assert "json-card[href]" in html
    assert "normalizeCardUrl" in html
    assert "local_page" in html
    assert "originalUrl" in html
    assert "decodeHtmlEntities" in html
    assert "renderForwardCard" in html
    assert "/api/forward" in html
    assert "renderLocalMediaParts" in html
    assert "renderLocalMediaAsset" in html
    assert "renderAvatar" in html
    assert "qlogo.cn" not in html
    assert "responseErrorMessage" in html
    assert "imagePreviewModal" in html
    assert "openImagePreview" in html
    assert "media-image-button" in html
    assert "media-missing" in html
    assert "renderMissingMediaChip" in html
    assert "plainMessagePreview" in html
    assert "findReplyMessage" in html
    assert "replyPreviewText" in html
    assert "reply_preview_text" in html
    assert "external_message_id" in html
    assert "media-file" in html
    assert "room.display_name" in html
    assert "room.avatar_path" in html
    assert "roomDisplayName" in html
    assert "sender_avatar_path" in html
    assert "账号设置" in html
    assert "adapterId" in html
    assert "saveAdapter" in html
    assert "deleteAdapter" in html
    assert "POST" in html
    assert "/api/adapters" in html
    assert "PATCH" in html
    assert "DELETE" in html
    assert "green/red/gray" in html
    assert "高级过滤导出" in html
    assert "openExportDialog" in html
    assert "exportModal" in html
    assert "offlineAuditModal" in html
    assert "openOfflineAuditDialog" in html
    assert "/api/offline/audit" in html
    assert "repairOfflineAudit" in html
    assert "/api/offline/repair" in html
    assert "profile_avatars" in html
    assert "repaired_profile_avatars" in html
    assert "exportRobotId" in html
    assert "downloadExportPackage" in html
    assert "/api/export" in html
    assert "robot_id" in html
    assert "room_id" in html
    assert "start_timestamp" in html
    assert "end_timestamp" in html
    assert "chat-audit-export" in html
    assert "导入 JSON" in html
    assert "openImportDialog" in html
    assert "importModal" in html
    assert "importPackageText" in html
    assert "importValidationReport" in html
    assert "validateImportPackage" in html
    assert "submitImportPackage" in html
    assert "chatAuditAdminApiToken" in html
    assert "Authorization" in html
    assert "/api/import/validate" in html
    assert "/api/import" in html
    assert "diff.messages" in html
    assert "media_files" in html
    assert "新增/更新/不变" in html
    assert "媒体文件" in html
