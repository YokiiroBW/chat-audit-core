from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.import_contract import (
    IMPORT_BATCH_MODES,
    IMPORT_SOURCE_STATUSES,
    MEDIA_ARCHIVE_STATES,
    MEDIA_SOURCE_STATES,
    SQL_INTEGER_MAX,
    SQL_INTEGER_MIN,
    validate_media_reference_assets,
)


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
    platform: str = Field(min_length=1, max_length=20, description="Source platform, for example qq or custom.")
    config_json: str | None = Field(default=None, description="Optional adapter configuration serialized as JSON.")
    status: str = Field(default="gray", min_length=1, max_length=20, description="Display/status flag: green, red, or gray.")
    current_robot_id: str | None = Field(default=None, max_length=64, description="Robot profile currently bound to this adapter.")


class AdapterUpdateRequest(BaseModel):
    """Partial adapter update payload."""

    platform: str | None = Field(default=None, min_length=1, max_length=20, description="Source platform, for example qq or custom.")
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
    not_downloaded_media: int = 0
    not_downloaded_videos: int = 0
    thumbnail_only_videos: int = 0
    source_missing_media: int = 0
    media_parse_failures: int = 0
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
    # Set when the configured expression cannot be scheduled. ``enabled`` only
    # says backups are switched on, so without this the status reported a
    # healthy schedule while the loop was unable to run a single backup.
    cron_error: str | None = None
    next_run_at: str | None = None
    worker_healthy: bool = False
    active_job: dict[str, Any] | None = None


class BackupSettingsUpdateRequest(BaseModel):
    """Runtime auto-backup settings update."""

    model_config = ConfigDict(json_schema_extra={"examples": [{"cron": "0 3 * * *", "keep_latest": 7}]})

    cron: str | None = Field(default=None, max_length=64, description="Five-field cron expression, or off/disabled/none/false/0 to disable.")
    keep_latest: int | None = Field(default=None, ge=0, le=365, description="Number of auto-backup files to retain.")
    reset_to_env: bool = Field(default=False, description="Reset database-stored backup settings and use environment values.")


class BackupRunResponse(BaseModel):
    job_id: str
    state: str
    backup_type: str
    created_at: str | None = None
    updated_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    path: str | None = None
    filename: str | None = None
    size_bytes: int | None = None
    duration_seconds: float | None = None
    peak_rss_mib: float | None = None
    counts: dict[str, int] = Field(default_factory=dict)
    retention_removed: list[str] = Field(default_factory=list)
    error: str | None = None


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
    qq_number: str | None = None


class MessagePartResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    ordinal: int
    part_type: str
    text_content: str | None = None
    media_reference_id: int | None = None
    payload_json: str | None = None
    source_format: str | None = None
    render_status: str


class MessageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    msg_hash: str
    platform: str
    room_id: str
    message_type: str
    external_message_id: str | None = None
    external_message_aliases: list[str] = Field(default_factory=list)
    sender_id: str
    sender_qq_number: str | None = None
    sender_display_name: str | None = None
    sender_avatar_path: str | None = None
    is_outgoing: bool | None = None
    nickname: str | None = None
    raw_message: str
    local_message: str
    timestamp: int
    source_sequence: int | None = None
    reply_to_message_id: str | None = None
    reply_preview_text: str | None = None
    parts: list[MessagePartResponse] = Field(default_factory=list)
    media: list["MessageMediaReferenceResponse"] = Field(default_factory=list)
    import_sources: list["MessageImportSourceResponse"] = Field(default_factory=list)


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
    platform: str = Field(min_length=1, max_length=20, description="Source platform, for example qq or custom.")
    room_id: str = Field(min_length=1, max_length=64, description="Group id or private peer id.")
    message_type: str = Field(min_length=1, max_length=20, description="group or private.")
    sender_id: str = Field(min_length=1, max_length=64, description="Original sender id.")
    is_outgoing: bool | None = Field(default=None, description="Whether the message was sent by the configured QQ account.")
    canonical_sender_id: str | None = Field(default=None, max_length=64)
    canonical_room_id: str | None = Field(default=None, max_length=64)
    source_event_type: str | None = Field(default=None, max_length=32)
    nickname: str | None = Field(default=None, max_length=128, description="Sender display name when available.")
    raw_message: str = Field(min_length=1, description="Raw message content, including CQ segments if present.")
    local_message: str | None = Field(default=None, description="Optional already-localized message content.")
    timestamp: int = Field(
        ge=SQL_INTEGER_MIN,
        le=SQL_INTEGER_MAX,
        description="Message timestamp in Unix seconds.",
    )
    source_sequence: int | None = Field(
        default=None,
        ge=0,
        le=SQL_INTEGER_MAX,
        description="Source platform sequence used to order messages sharing the same timestamp.",
    )
    message_id: str | None = Field(default=None, max_length=64, description="Source-platform message id used for reply jumps and deduplication.")
    message_segments: list[dict[str, Any]] = Field(default_factory=list, max_length=100)


class MessageIngestResponse(BaseModel):
    msg_hash: str | None = None
    skipped: bool = False
    skip_reason: str | None = None


class MessageImportSourceResponse(BaseModel):
    source_id: str
    source_type: str
    device_name: str | None = None
    qq_version: str | None = None
    schema_version: str | None = None
    imported_at: datetime
    last_seen_at: datetime


class MessageMediaReferenceRequest(BaseModel):
    ordinal: int = Field(default=0, ge=0, le=SQL_INTEGER_MAX)
    media_type: str = Field(min_length=1, max_length=20)
    source_state: str
    archive_state: str
    asset_file_hash: str | None = Field(default=None, max_length=64)
    thumbnail_file_hash: str | None = Field(default=None, max_length=64)
    file_name: str | None = Field(default=None, max_length=512)
    file_ext: str | None = Field(default=None, max_length=32)
    declared_file_size: int | None = Field(default=None, ge=0, le=SQL_INTEGER_MAX)
    actual_file_size: int | None = Field(default=None, ge=0, le=SQL_INTEGER_MAX)
    source_md5: str | None = Field(default=None, max_length=128)
    source_sha1: str | None = Field(default=None, max_length=128)
    source_uuid: str | None = Field(default=None, max_length=255)
    content_sha256: str | None = Field(default=None, max_length=64)
    source_local_path: str | None = None
    duration_ms: int | None = Field(default=None, ge=0, le=SQL_INTEGER_MAX)
    width: int | None = Field(default=None, ge=0, le=SQL_INTEGER_MAX)
    height: int | None = Field(default=None, ge=0, le=SQL_INTEGER_MAX)
    failure_code: str | None = Field(default=None, max_length=64)
    failure_detail: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_contract(self):
        if self.source_state not in MEDIA_SOURCE_STATES:
            raise ValueError(f"unsupported source_state: {self.source_state}")
        if self.archive_state not in MEDIA_ARCHIVE_STATES:
            raise ValueError(f"unsupported archive_state: {self.archive_state}")
        validate_media_reference_assets(
            source_state=self.source_state,
            archive_state=self.archive_state,
            asset_file_hash=self.asset_file_hash,
            thumbnail_file_hash=self.thumbnail_file_hash,
        )
        return self


class MessageMediaReferenceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    ordinal: int
    media_type: str
    source_state: str
    archive_state: str
    asset_file_hash: str | None = None
    thumbnail_file_hash: str | None = None
    asset_local_path: str | None = None
    thumbnail_local_path: str | None = None
    file_name: str | None = None
    file_ext: str | None = None
    declared_file_size: int | None = None
    actual_file_size: int | None = None
    source_md5: str | None = None
    source_sha1: str | None = None
    source_uuid: str | None = None
    content_sha256: str | None = None
    duration_ms: int | None = None
    width: int | None = None
    height: int | None = None
    failure_code: str | None = None
    failure_detail: str | None = None
    availability_reason: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    first_seen_at: datetime
    last_checked_at: datetime
    archived_at: datetime | None = None


class ImportSourceCreateRequest(BaseModel):
    id: str | None = Field(default=None, min_length=1, max_length=64)
    source_type: str = Field(default="qqnt_local_db", min_length=1, max_length=32)
    platform: str = Field(default="qq", min_length=1, max_length=20)
    account_id: str = Field(min_length=1, max_length=64)
    device_id: str = Field(min_length=1, max_length=128)
    device_name: str | None = Field(default=None, max_length=128)
    qq_version: str | None = Field(default=None, max_length=64)
    schema_version: str | None = Field(default=None, max_length=64)
    status: str = Field(default="active")
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_status(self):
        if self.status not in IMPORT_SOURCE_STATUSES:
            raise ValueError(f"unsupported import source status: {self.status}")
        return self


class ImportSourceResponse(BaseModel):
    id: str
    source_type: str
    platform: str
    account_id: str
    device_id: str
    device_name: str | None = None
    qq_version: str | None = None
    schema_version: str | None = None
    status: str
    first_seen_at: datetime
    last_seen_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


class ImportBatchCreateRequest(BaseModel):
    id: str | None = Field(default=None, min_length=1, max_length=64)
    source_id: str = Field(min_length=1, max_length=64)
    mode: str
    detail: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_mode(self):
        if self.mode not in IMPORT_BATCH_MODES:
            raise ValueError(f"unsupported import batch mode: {self.mode}")
        return self


class ImportBatchFinishRequest(BaseModel):
    detail: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = Field(default=None, max_length=64)
    error_detail: str | None = None
    partial: bool = False


class ImportBatchResponse(BaseModel):
    id: str
    source_id: str
    mode: str
    status: str
    started_at: datetime
    completed_at: datetime | None = None
    scanned_messages: int
    inserted_messages: int
    updated_messages: int
    skipped_messages: int
    uploaded_media: int
    not_downloaded_media: int
    missing_media: int
    failed_media: int
    detail: dict[str, Any] = Field(default_factory=dict)


class MessageSourceRecordRequest(BaseModel):
    source_table: str = Field(min_length=1, max_length=128)
    source_key: str = Field(min_length=1, max_length=512)
    platform_message_id: str | None = Field(default=None, min_length=1, max_length=64)
    schema_version: str | None = Field(default=None, max_length=64)
    raw_columns: dict[str, Any] = Field(default_factory=dict)
    raw_40800_protobuf: str | None = None
    raw_40900_protobuf: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ImportMessageItemRequest(BaseModel):
    message: MessageIngestRequest
    source_record: MessageSourceRecordRequest
    media: list[MessageMediaReferenceRequest] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def require_stable_message_id(self):
        if not self.message.message_id:
            raise ValueError("Collector batch messages require a stable message.message_id")
        return self


class ImportBatchMessagesRequest(BaseModel):
    messages: list[ImportMessageItemRequest] = Field(min_length=1, max_length=500)


class ImportBatchItemResponse(BaseModel):
    index: int
    message_id: str | None = None
    msg_hash: str | None = None
    status: str
    error: str | None = None


class ImportBatchMessagesResponse(BaseModel):
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    failed: int = 0
    replayed: bool = False
    items: list[ImportBatchItemResponse] = Field(default_factory=list)


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
    severity: str = "error"


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
    not_downloaded_media: int = 0
    not_downloaded_videos: int = 0
    thumbnail_only_videos: int = 0
    source_missing_media: int = 0
    media_parse_failures: int = 0
    media_hash_mismatches: int = 0
    reason_summary: dict[str, int] = Field(default_factory=dict)
    issues: list[OfflineAuditIssueResponse]


class OfflineRepairResponse(BaseModel):
    scanned_messages: int
    repaired_media_assets: int
    repaired_media_files: int
    repaired_file_sizes: int
    repaired_profile_avatars: int
    unrepaired_media_files: int = 0
    media_hash_mismatches: int = 0
    repaired_paths: list[str]


class ImportResultResponse(BaseModel):
    messages: int
    robot_messages: int
    media_assets: int
    import_sources: int = 0
    import_batches: int = 0
    message_source_records: int = 0
    message_media_references: int = 0


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
