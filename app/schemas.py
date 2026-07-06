from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class HealthResponse(BaseModel):
    status: str = Field(default="ok")
    app: str


class AdapterResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    platform: str
    config_json: str | None = None
    status: str
    current_robot_id: str | None = None


class AdapterCreateRequest(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    platform: str = Field(min_length=1, max_length=20)
    config_json: str | None = None
    status: str = Field(default="gray", min_length=1, max_length=20)
    current_robot_id: str | None = Field(default=None, max_length=64)


class AdapterUpdateRequest(BaseModel):
    platform: str | None = Field(default=None, min_length=1, max_length=20)
    config_json: str | None = None
    status: str | None = Field(default=None, min_length=1, max_length=20)
    current_robot_id: str | None = Field(default=None, max_length=64)


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
    list_mode: str = Field(default="none", min_length=1, max_length=20)
    capture_text: bool = True
    capture_image: bool = True
    capture_voice: bool = True
    capture_video: bool = True
    capture_file: bool = False


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
    cron: str | None = Field(default=None, max_length=64)
    keep_latest: int | None = Field(default=None, ge=0, le=365)
    reset_to_env: bool = False


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
    name: str = Field(min_length=1, max_length=128)
    role: str = Field(default="viewer", min_length=1, max_length=20)


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
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=8, max_length=256)
    role: str = Field(default="viewer", min_length=1, max_length=20)
    display_name: str | None = Field(default=None, max_length=128)


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
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


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
    robot_id: str = Field(min_length=1, max_length=64)
    platform: str = Field(min_length=1, max_length=20)
    room_id: str = Field(min_length=1, max_length=64)
    message_type: str = Field(min_length=1, max_length=20)
    sender_id: str = Field(min_length=1, max_length=64)
    nickname: str | None = Field(default=None, max_length=128)
    raw_message: str = Field(min_length=1)
    local_message: str | None = None
    timestamp: int
    message_id: str | None = Field(default=None, max_length=64)


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
