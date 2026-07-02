from pydantic import BaseModel, ConfigDict, Field


class HealthResponse(BaseModel):
    status: str = Field(default="ok")
    app: str


class AdapterResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    platform: str
    status: str


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
