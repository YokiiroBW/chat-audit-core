from sqlalchemy import Column, DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import declarative_base

from app.time_utils import utc_now

Base = declarative_base()


class Adapter(Base):
    """协议端口配置表。"""

    __tablename__ = "adapters"

    id = Column(String(64), primary_key=True)
    platform = Column(String(20), nullable=False)
    config_json = Column(Text, nullable=True)
    status = Column(String(20), default="gray", nullable=False)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now, nullable=False)


class Message(Base):
    """全局消息池表。"""

    __tablename__ = "messages"

    msg_hash = Column(String(64), primary_key=True)
    platform = Column(String(20), nullable=False)
    room_id = Column(String(64), nullable=False)
    message_type = Column(String(20), nullable=False)
    sender_id = Column(String(64), nullable=False)
    nickname = Column(String(128), nullable=True)
    raw_message = Column(Text, nullable=False)
    local_message = Column(Text, nullable=False)
    timestamp = Column(Integer, nullable=False, index=True)
    created_at = Column(DateTime, default=utc_now, nullable=False)

    __table_args__ = (Index("idx_room_timestamp", "room_id", "timestamp"),)


class RobotMessage(Base):
    """主视角关联表。"""

    __tablename__ = "robot_messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    robot_id = Column(String(64), nullable=False, index=True)
    msg_hash = Column(String(64), nullable=False, index=True)

    __table_args__ = (UniqueConstraint("robot_id", "msg_hash", name="uq_robot_message_view"),)


class MediaAsset(Base):
    """媒体资产索引表。"""

    __tablename__ = "media_assets"

    file_hash = Column(String(64), primary_key=True)
    file_type = Column(String(20), nullable=False)
    file_size = Column(Integer, nullable=False)
    local_path = Column(String(255), nullable=False)
    created_at = Column(DateTime, default=utc_now, nullable=False)
