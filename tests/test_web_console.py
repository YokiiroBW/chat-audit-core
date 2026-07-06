from httpx import ASGITransport, AsyncClient
import pytest

from app.main import app


@pytest.mark.asyncio
async def test_web_console_index_serves_three_column_dashboard():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/")
        css_response = await client.get("/assets/app.css")
        js_response = await client.get("/assets/app.js")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    html = response.text
    assert '<link rel="stylesheet" href="/assets/app.css" />' in html
    assert '<script src="/assets/app.js" defer></script>' in html

    assert css_response.status_code == 200
    assert js_response.status_code == 200
    assert "text/css" in css_response.headers["content-type"]
    assert "javascript" in js_response.headers["content-type"]

    page_bundle = "\n".join([html, css_response.text, js_response.text])
    assert "社交资产多租户审计控制台" in html
    assert "AUDIT V4" in html
    assert "unpkg.com" not in page_bundle
    assert "cdn.tailwindcss.com" not in page_bundle
    assert "ElementPlus" not in page_bundle
    assert "Vue" not in page_bundle
    assert "/api/adapters" in page_bundle
    assert "/api/bots" in page_bundle
    assert "/api/dashboard" in page_bundle
    assert "/api/backup/status" in page_bundle
    assert "/api/backup/run" in page_bundle
    assert "/api/backup/settings" in page_bundle
    assert "dashboardSummary" in page_bundle
    assert "renderDashboard" in page_bundle
    assert "/api/rooms" in page_bundle
    assert "/api/messages" in page_bundle
    assert "/api/search" in page_bundle
    assert "performSearch" in page_bundle
    assert "searchResults" in page_bundle
    assert "selectSearchResult" in page_bundle
    assert "writeRouteState" in page_bundle
    assert "restoreRouteState" in page_bundle
    assert "URLSearchParams(window.location.hash" in page_bundle
    assert "hashchange" in page_bundle
    assert "popstate" in page_bundle
    assert "搜索结果" in page_bundle
    assert "before_timestamp" in page_bundle
    assert "static/storage" in page_bundle
    assert "parseCQSegment" in page_bundle
    assert "renderCQJsonCard" in page_bundle
    assert "打开卡片网页" in page_bundle
    assert "json-card[href]" in page_bundle
    assert "normalizeCardUrl" in page_bundle
    assert "pickPreferredCardPageUrl" in page_bundle
    assert "isQqMiniappShellUrl" in page_bundle
    assert "local_page" in page_bundle
    assert "originalUrl" in page_bundle
    assert "decodeHtmlEntities" in page_bundle
    assert "renderForwardCard" in page_bundle
    assert "/api/forward" in page_bundle
    assert "forward-toggle" in page_bundle
    assert ".forward-card.expanded > .forward-toggle" in page_bundle
    assert "renderLocalMediaParts" in page_bundle
    assert "renderLocalMediaAsset" in page_bundle
    assert "renderAvatar" in page_bundle
    assert "qlogo.cn" not in page_bundle
    assert "responseErrorMessage" in page_bundle
    assert "requestWithRetry" in page_bundle
    assert "friendlyHttpErrorMessage" in page_bundle
    assert "friendlyNetworkErrorMessage" in page_bundle
    assert "shouldRetryResponse" in page_bundle
    assert "guardedHandler" in page_bundle
    assert "imagePreviewModal" in page_bundle
    assert "openImagePreview" in page_bundle
    assert "media-image-button" in page_bundle
    assert "bubble-media-only" in page_bundle
    assert "finalizeMessageBubbleLayout" in page_bundle
    assert "message-link" in page_bundle
    assert "appendLinkedText" in page_bundle
    assert "normalizeSafeUrl" in page_bundle
    assert "normalizeSafeMediaSrc" in page_bundle
    assert "validationError" in page_bundle
    assert "SAFE_ADAPTER_STATUSES" in page_bundle
    assert "SAFE_CAPTURE_LIST_MODES" in page_bundle
    assert "MAX_IMPORT_PACKAGE_BYTES" in page_bundle
    assert "parseJsonObjectInput" in page_bundle
    assert "normalizedTimestamp" in page_bundle
    assert "media-missing" in page_bundle
    assert "renderMissingMediaChip" in page_bundle
    assert "plainMessagePreview" in page_bundle
    assert "findReplyMessage" in page_bundle
    assert "replyPreviewText" in page_bundle
    assert "jumpToReplyMessage" in page_bundle
    assert "around_message_id" in page_bundle
    assert "reply-jump-highlight" in page_bundle
    assert "replyJumpMissingId" in page_bundle
    assert "原消息未缓存或已被清理" in page_bundle
    assert "reply_preview_text" in page_bundle
    assert "external_message_id" in page_bundle
    assert "acc.avatar_path" in page_bundle
    assert "media-file" in page_bundle
    assert "room.display_name" in page_bundle
    assert "room.avatar_path" in page_bundle
    assert "roomDisplayName" in page_bundle
    assert "sender_avatar_path" in page_bundle
    assert "账号设置" in page_bundle
    assert "自动备份" in page_bundle
    assert "runManualBackup" in page_bundle
    assert "backupStatusReport" in page_bundle
    assert "backupCronInput" in page_bundle
    assert "backupKeepLatestInput" in page_bundle
    assert "saveBackupSettings" in page_bundle
    assert "resetBackupSettings" in page_bundle
    assert "管理令牌" in page_bundle
    assert "createAdminToken" in page_bundle
    assert "revokeAdminToken" in page_bundle
    assert "refreshAdminTokens" in page_bundle
    assert "adminTokenCreateReport" in page_bundle
    assert "adminTokenList" in page_bundle
    assert "/api/admin/tokens" in page_bundle
    assert "adapterId" in page_bundle
    assert "saveAdapter" in page_bundle
    assert "deleteAdapter" in page_bundle
    assert "POST" in page_bundle
    assert "/api/adapters" in page_bundle
    assert "PATCH" in page_bundle
    assert "DELETE" in page_bundle
    assert "green/red/gray" in page_bundle
    assert "高级过滤导出" in page_bundle
    assert "openExportDialog" in page_bundle
    assert "exportModal" in page_bundle
    assert "offlineAuditModal" in page_bundle
    assert "openOfflineAuditDialog" in page_bundle
    assert "/api/offline/audit" in page_bundle
    assert "repairOfflineAudit" in page_bundle
    assert "/api/offline/repair" in page_bundle
    assert "profile_avatars" in page_bundle
    assert "repaired_profile_avatars" in page_bundle
    assert "reason_summary" in page_bundle
    assert "缺失原因汇总" in page_bundle
    assert "exportRobotId" in page_bundle
    assert "downloadExportPackage" in page_bundle
    assert "requestBlob" in page_bundle
    assert "compressed', 'true'" in page_bundle
    assert "/api/export" in page_bundle
    assert "robot_id" in page_bundle
    assert "room_id" in page_bundle
    assert "start_timestamp" in page_bundle
    assert "end_timestamp" in page_bundle
    assert "chat-audit-export" in page_bundle
    assert ".json.gz" in page_bundle
    assert "导入 JSON" in page_bundle
    assert "openImportDialog" in page_bundle
    assert "importModal" in page_bundle
    assert "importPackageText" in page_bundle
    assert "importValidationReport" in page_bundle
    assert "validateImportPackage" in page_bundle
    assert "submitImportPackage" in page_bundle
    assert "chatAuditAdminApiToken" in page_bundle
    assert "Authorization" in page_bundle
    assert "chat_audit_csrf" in page_bundle
    assert "X-CSRF-Token" in page_bundle
    assert "csrfHeaders" in page_bundle
    assert "/api/auth/login" in page_bundle
    assert "/api/auth/me" in page_bundle
    assert "/api/auth/logout" in page_bundle
    assert "/api/admin/users" in page_bundle
    assert "/api/admin/sessions" in page_bundle
    assert "/api/admin/users/${encodeURIComponent(userId)}/password" in page_bundle
    assert "/api/admin/sessions/${encodeURIComponent(sessionId)}" in page_bundle
    assert "loginWithPassword" in page_bundle
    assert "logoutAuth" in page_bundle
    assert "refreshAuthIdentity" in page_bundle
    assert "createAdminUser" in page_bundle
    assert "revokeAdminUser" in page_bundle
    assert "refreshAdminSessions" in page_bundle
    assert "resetAdminUserPassword" in page_bundle
    assert "revokeAdminSession" in page_bundle
    assert "applyRoleUi" in page_bundle
    assert "canRole('admin')" in page_bundle
    assert "authStatusReport" in page_bundle
    assert "capturePolicyList" in page_bundle
    assert "renderCapturePolicies" in page_bundle
    assert "refreshCapturePolicies" in page_bundle
    assert "saveCapturePolicy" in page_bundle
    assert "/api/bots/${encodeURIComponent(state.currentRobot.id)}/capture-targets" in page_bundle
    assert "/capture-policies/" in page_bundle
    assert "capture_file" in page_bundle
    assert "文件包/文档" in page_bundle
    assert "adminUserList" in page_bundle
    assert "adminSessionList" in page_bundle
    assert "/api/import/validate" in page_bundle
    assert "/api/import" in page_bundle
    assert "signature" in page_bundle
    assert "report.source" in page_bundle
    assert "diff.messages" in page_bundle
    assert "media_files" in page_bundle
    assert "新增/更新/不变" in page_bundle
    assert "媒体文件" in page_bundle
