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


class AdapterCreateRequest(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    platform: str = Field(min_length=1, max_length=20)
    config_json: str | None = None
    status: str = Field(default="gray", min_length=1, max_length=20)


class AdapterUpdateRequest(BaseModel):
    platform: str | None = Field(default=None, min_length=1, max_length=20)
    config_json: str | None = None
    status: str | None = Field(default=None, min_length=1, max_length=20)


class RoomResponse(BaseModel):
    room_id: str
    last_timestamp: int


class MessageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    msg_hash: str
    platform: str
    room_id: str
    message_type: str
    sender_id: str
    nickname: str | None = None
    raw_message: str
    local_message: str
    timestamp: int


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
