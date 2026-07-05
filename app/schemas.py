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
    status: str
    source_adapter_id: str | None = None
    last_seen_at: datetime | None = None


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


class MediaBackfillFailureResponse(BaseModel):
    msg_hash: str
    kind: str
    target: str
    reason: str


class MediaBackfillResponse(BaseModel):
    scanned: int
    candidates: int
    updated: int
    unchanged: int
    failed: int
    media_failed: int
    forward_failed: int
    failures: list[MediaBackfillFailureResponse]


class OfflineAuditIssueResponse(BaseModel):
    kind: str
    target: str
    reason: str
    msg_hash: str | None = None


class OfflineAuditResponse(BaseModel):
    offline_ready: bool
    messages_scanned: int
    media_assets_checked: int
    remote_media_urls: int
    uncached_card_pages: int
    uncached_forwards: int
    missing_media_assets: int
    missing_media_files: int
    issues: list[OfflineAuditIssueResponse]


class OfflineRepairResponse(BaseModel):
    scanned_messages: int
    repaired_media_assets: int
    repaired_media_files: int
    repaired_file_sizes: int
    repaired_paths: list[str]


class ImportResultResponse(BaseModel):
    messages: int
    robot_messages: int
    media_assets: int


class ImportValidationResponse(BaseModel):
    valid: bool
    schema_: str | None = Field(default=None, alias="schema")
    checksum_valid: bool | None = None
    errors: list[str]
    counts: dict[str, int]
    media_files: dict[str, int] = Field(default_factory=dict)
    diff: dict[str, dict[str, int]] = Field(default_factory=dict)
