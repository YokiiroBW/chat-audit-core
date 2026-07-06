from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class HealthResponse(BaseModel):
    status: str = Field(default="ok")
    app: str
    checks: dict[str, str] = Field(default_factory=dict)


class AdapterResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    platform: str
    config_json: str | None = None
    status: str
    current_robot_id: str | None = None


class AdapterCreateRequest(BaseModel):
    """Adapter registration payload for a QQ/NapCat or future platform connector."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "id": "napcat-26109",
                    "platform": "qq",
                    "status": "gray",
                    "config_json": "{\"reverse_ws_host\":\"0.0.0.0\",\"reverse_ws_port\":26109}",
                }
            ]
        }
    )

    id: str = Field(min_length=1, max_length=64, description="Stable adapter id, usually the connector name or self_id.")
    platform: str = Field(min_length=1, max_length=20, description="Source platform, for example qq or wechat.")
    config_json: str | None = Field(default=None, description="Optional adapter configuration serialized as JSON.")
    status: str = Field(default="gray", min_length=1, max_length=20, description="Display/status flag: green, red, or gray.")
    current_robot_id: str | None = Field(default=None, max_length=64, description="Robot profile currently bound to this adapter.")


class AdapterUpdateRequest(BaseModel):
    """Partial adapter update payload."""

    platform: str | None = Field(default=None, min_length=1, max_length=20, description="Source platform, for example qq or wechat.")
    config_json: str | None = Field(default=None, description="Optional adapter configuration serialized as JSON.")
    status: str | None = Field(default=None, min_length=1, max_length=20, description="Display/status flag: green, red, or gray.")
    current_robot_id: str | None = Field(default=None, max_length=64, description="Robot profile currently bound to this adapter.")


class BotProfileResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    platform: str
    display_name: str | None = None
    avatar_path: str | None = None
    status: str
    source_adapter_id: str | None = None
    last_seen_at: datetime | None = None


class CaptureTargetPolicyUpdateRequest(BaseModel):
    """Per-room or per-private-chat capture policy."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "list_mode": "whitelist",
                    "capture_text": True,
                    "capture_image": True,
                    "capture_voice": True,
                    "capture_video": True,
                    "capture_file": False,
                }
            ]
        }
    )

    list_mode: str = Field(default="none", min_length=1, max_length=20, description="none captures by default, blacklist skips target, whitelist captures only listed targets.")
    capture_text: bool = Field(default=True, description="Capture text, links, cards, and merged forwards.")
    capture_image: bool = Field(default=True, description="Capture image and animated image messages.")
    capture_voice: bool = Field(default=True, description="Capture voice messages.")
    capture_video: bool = Field(default=True, description="Capture video messages.")
    capture_file: bool = Field(default=False, description="Capture generic files such as zip/apk/installers. Disabled by default.")


class CaptureTargetPolicyResponse(BaseModel):
    id: int | None = None
    robot_id: str
    target_type: str
    target_id: str
    list_mode: str = "none"
    capture_text: bool = True
    capture_image: bool = True
    capture_voice: bool = True
    capture_video: bool = True
    capture_file: bool = False
    display_name: str | None = None
    avatar_path: str | None = None
    last_timestamp: int | None = None
    updated_at: datetime | None = None


class CaptureTargetSettingResponse(BaseModel):
    robot_id: str
    target_type: str
    target_id: str
    display_name: str | None = None
    avatar_path: str | None = None
    last_timestamp: int | None = None
    policy: CaptureTargetPolicyResponse | None = None


class DashboardResponse(BaseModel):
    bots: int
    rooms: int
    messages: int
    robot_views: int
    media_assets: int
    media_bytes: int
    backups: int
    latest_backup: str | None = None


class BackupStatusResponse(BaseModel):
    enabled: bool
    cron: str
    keep_latest: int
    backup_root: str
    backups: int
    latest_backup: str | None = None
    config_source: str = "env"
    cron_source: str = "env"
    keep_latest_source: str = "env"


class BackupSettingsUpdateRequest(BaseModel):
    """Runtime auto-backup settings update."""

    model_config = ConfigDict(json_schema_extra={"examples": [{"cron": "0 3 * * *", "keep_latest": 7}]})

    cron: str | None = Field(default=None, max_length=64, description="Five-field cron expression, or off/disabled/none/false/0 to disable.")
    keep_latest: int | None = Field(default=None, ge=0, le=365, description="Number of auto-backup files to retain.")
    reset_to_env: bool = Field(default=False, description="Reset database-stored backup settings and use environment values.")


class BackupRunResponse(BaseModel):
    path: str
    filename: str


class AuditLogResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    action: str
    status: str
    actor: str | None = None
    ip_address: str | None = None
    target: str | None = None
    detail_json: str | None = None
    created_at: datetime | None = None


class AdminTokenCreateRequest(BaseModel):
    """Create an operator/admin API token."""

    model_config = ConfigDict(json_schema_extra={"examples": [{"name": "nas-ops", "role": "operator"}]})

    name: str = Field(min_length=1, max_length=128, description="Human-readable token name.")
    role: str = Field(default="viewer", min_length=1, max_length=20, description="viewer, operator, or admin.")


class AdminTokenResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    role: str
    token_prefix: str
    status: str
    created_at: datetime | None = None
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None
    token: str | None = None


class AdminTokenRotateResponse(AdminTokenResponse):
    token: str | None = None


class AdminUserCreateRequest(BaseModel):
    """Create a database-managed admin console user."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {"username": "ops", "password": "change-me-strong-password", "role": "operator", "display_name": "Ops"}
            ]
        }
    )

    username: str = Field(min_length=1, max_length=64, description="Login username.")
    password: str = Field(min_length=8, max_length=256, description="Initial password. Stored with bcrypt.")
    role: str = Field(default="viewer", min_length=1, max_length=20, description="viewer, operator, or admin.")
    display_name: str | None = Field(default=None, max_length=128, description="Optional display name.")


class AdminUserPasswordResetRequest(BaseModel):
    password: str = Field(min_length=8, max_length=256)


class AdminUserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    display_name: str | None = None
    role: str
    status: str
    created_at: datetime | None = None
    last_login_at: datetime | None = None
    revoked_at: datetime | None = None


class AdminSessionResponse(BaseModel):
    id: int
    user_id: int
    username: str
    role: str
    token_prefix: str
    status: str
    created_at: datetime | None = None
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None


class AuthLoginRequest(BaseModel):
    """Password login request for database-managed admin users."""

    model_config = ConfigDict(json_schema_extra={"examples": [{"username": "ops", "password": "change-me-strong-password"}]})

    username: str = Field(min_length=1, max_length=64, description="Admin username.")
    password: str = Field(min_length=1, max_length=256, description="Admin password.")


class AuthLoginResponse(BaseModel):
    token: str
    token_type: str = "bearer"
    user: AdminUserResponse


class AuthMeResponse(BaseModel):
    actor: str
    role: str
    user_id: int | None = None
    session_id: int | None = None
    username: str | None = None


class MigrationStatusResponse(BaseModel):
    version: str
    description: str
    applied: bool
    applied_at: datetime | None = None


class RuntimeStatusResponse(BaseModel):
    media_transcode_enabled: bool
    ffmpeg_bin: str
    ffmpeg_library_path: str = ""
    ffmpeg_available: bool
    ffmpeg_path: str | None = None
    ffmpeg_version: str | None = None
    ffmpeg_error: str | None = None
    voice_ext: str
    video_ext: str


class RoomResponse(BaseModel):
    room_id: str
    last_timestamp: int
    message_type: str | None = None
    display_name: str | None = None
    avatar_path: str | None = None


class MessageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    msg_hash: str
    platform: str
    room_id: str
    message_type: str
    external_message_id: str | None = None
    sender_id: str
    sender_display_name: str | None = None
    sender_avatar_path: str | None = None
    nickname: str | None = None
    raw_message: str
    local_message: str
    timestamp: int
    reply_to_message_id: str | None = None
    reply_preview_text: str | None = None


class MessageIngestRequest(BaseModel):
    """External normalized message ingestion payload."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "robot_id": "1449801200",
                    "platform": "qq",
                    "room_id": "955973452",
                    "message_type": "group",
                    "sender_id": "389772436",
                    "nickname": "Alice",
                    "raw_message": "hello [CQ:image,file=a.jpg,url=https://example.test/a.jpg]",
                    "timestamp": 1783317330,
                    "message_id": "762197037",
                }
            ]
        }
    )

    robot_id: str = Field(min_length=1, max_length=64, description="Robot account id from whose perspective this message is captured.")
    platform: str = Field(min_length=1, max_length=20, description="Source platform, for example qq or wechat.")
    room_id: str = Field(min_length=1, max_length=64, description="Group id or private peer id.")
    message_type: str = Field(min_length=1, max_length=20, description="group or private.")
    sender_id: str = Field(min_length=1, max_length=64, description="Original sender id.")
    nickname: str | None = Field(default=None, max_length=128, description="Sender display name when available.")
    raw_message: str = Field(min_length=1, description="Raw message content, including CQ segments if present.")
    local_message: str | None = Field(default=None, description="Optional already-localized message content.")
    timestamp: int = Field(description="Message timestamp in Unix seconds.")
    message_id: str | None = Field(default=None, max_length=64, description="Source-platform message id used for reply jumps and deduplication.")


class MessageIngestResponse(BaseModel):
    msg_hash: str | None = None
    skipped: bool = False
    skip_reason: str | None = None


class ExternalMediaUploadResponse(BaseModel):
    local_path: str
    media_type: str
    file_name: str | None = None
    file_size: int
    file_hash: str


class MediaBackfillFailureResponse(BaseModel):
    msg_hash: str
    kind: str
    target: str
    reason: str
    label: str | None = None
    action: str | None = None


class MediaBackfillResponse(BaseModel):
    scanned: int
    candidates: int
    updated: int
    unchanged: int
    failed: int
    media_failed: int
    forward_failed: int
    reason_summary: dict[str, int] = Field(default_factory=dict)
    failures: list[MediaBackfillFailureResponse]


class OfflineAuditIssueResponse(BaseModel):
    kind: str
    target: str
    reason: str
    msg_hash: str | None = None
    label: str | None = None
    action: str | None = None


class OfflineAuditResponse(BaseModel):
    offline_ready: bool
    messages_scanned: int
    media_assets_checked: int
    profile_avatars_checked: int
    remote_media_urls: int
    uncached_card_pages: int
    uncached_forwards: int
    missing_profile_avatars: int
    missing_media_assets: int
    missing_media_files: int
    reason_summary: dict[str, int] = Field(default_factory=dict)
    issues: list[OfflineAuditIssueResponse]


class OfflineRepairResponse(BaseModel):
    scanned_messages: int
    repaired_media_assets: int
    repaired_media_files: int
    repaired_file_sizes: int
    repaired_profile_avatars: int
    repaired_paths: list[str]


class ImportResultResponse(BaseModel):
    messages: int
    robot_messages: int
    media_assets: int


class ImportValidationResponse(BaseModel):
    valid: bool
    schema_: str | None = Field(default=None, alias="schema")
    checksum_valid: bool | None = None
    signature_valid: bool | None = None
    source: dict | None = None
    errors: list[str]
    counts: dict[str, int]
    media_files: dict[str, int] = Field(default_factory=dict)
    diff: dict[str, dict[str, int]] = Field(default_factory=dict)
