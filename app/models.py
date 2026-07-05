from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
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
    current_robot_id = Column(String(64), nullable=True, index=True)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now, nullable=False)


class BotProfile(Base):
    """Discovered bot identity profile, independent from connection adapters."""

    __tablename__ = "bot_profiles"

    id = Column(String(64), primary_key=True)
    platform = Column(String(20), nullable=False)
    display_name = Column(String(128), nullable=True)
    status = Column(String(20), default="gray", nullable=False)
    source_adapter_id = Column(String(64), nullable=True, index=True)
    first_seen_at = Column(DateTime, default=utc_now, nullable=False)
    last_seen_at = Column(DateTime, default=utc_now, onupdate=utc_now, nullable=False)


class RoomProfile(Base):
    """Cached conversation metadata for local browsing."""

    __tablename__ = "room_profiles"

    room_id = Column(String(64), primary_key=True)
    platform = Column(String(20), nullable=False)
    display_name = Column(String(128), nullable=True)
    avatar_path = Column(String(255), nullable=True)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now, nullable=False)


class UserProfile(Base):
    """Cached user metadata for local avatars and private chats."""

    __tablename__ = "user_profiles"

    user_id = Column(String(64), primary_key=True)
    platform = Column(String(20), nullable=False)
    display_name = Column(String(128), nullable=True)
    avatar_path = Column(String(255), nullable=True)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now, nullable=False)


class Message(Base):
    """全局消息池表。"""

    __tablename__ = "messages"

    msg_hash = Column(String(64), primary_key=True)
    platform = Column(String(20), nullable=False)
    room_id = Column(String(64), nullable=False)
    message_type = Column(String(20), nullable=False)
    external_message_id = Column(String(64), nullable=True, index=True)
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


class AuditLog(Base):
    """Management operation audit log."""

    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    action = Column(String(64), nullable=False, index=True)
    status = Column(String(20), nullable=False)
    actor = Column(String(128), nullable=True)
    ip_address = Column(String(64), nullable=True)
    target = Column(String(255), nullable=True)
    detail_json = Column(Text, nullable=True)
    created_at = Column(DateTime, default=utc_now, nullable=False, index=True)


class AdminToken(Base):
    """Database-managed admin API token metadata."""

    __tablename__ = "admin_tokens"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(128), nullable=False)
    role = Column(String(20), nullable=False)
    token_hash = Column(String(64), nullable=False, unique=True, index=True)
    token_prefix = Column(String(16), nullable=False)
    status = Column(String(20), default="active", nullable=False, index=True)
    created_at = Column(DateTime, default=utc_now, nullable=False)
    last_used_at = Column(DateTime, nullable=True)
    revoked_at = Column(DateTime, nullable=True)


class AdminUser(Base):
    """Database-managed console user."""

    __tablename__ = "admin_users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(64), nullable=False, unique=True, index=True)
    display_name = Column(String(128), nullable=True)
    role = Column(String(20), nullable=False)
    password_hash = Column(String(255), nullable=False)
    status = Column(String(20), default="active", nullable=False, index=True)
    created_at = Column(DateTime, default=utc_now, nullable=False)
    last_login_at = Column(DateTime, nullable=True)
    revoked_at = Column(DateTime, nullable=True)


class AdminSession(Base):
    """Bearer login session for database-managed users."""

    __tablename__ = "admin_sessions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("admin_users.id"), nullable=False, index=True)
    token_hash = Column(String(64), nullable=False, unique=True, index=True)
    token_prefix = Column(String(16), nullable=False)
    status = Column(String(20), default="active", nullable=False, index=True)
    created_at = Column(DateTime, default=utc_now, nullable=False)
    last_used_at = Column(DateTime, nullable=True)
    revoked_at = Column(DateTime, nullable=True)


class SystemSetting(Base):
    """Database-managed runtime setting override."""

    __tablename__ = "system_settings"

    key = Column(String(128), primary_key=True)
    value_json = Column(Text, nullable=False)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now, nullable=False)


class SchemaMigration(Base):
    """Applied lightweight schema migration marker."""

    __tablename__ = "schema_migrations"

    version = Column(String(64), primary_key=True)
    description = Column(String(255), nullable=False)
    applied_at = Column(DateTime, default=utc_now, nullable=False)
