from sqlalchemy import Boolean, CheckConstraint, Column, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
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


class CaptureTargetPolicy(Base):
    """Per-bot capture scope and content-type policy for one conversation target."""

    __tablename__ = "capture_target_policies"

    id = Column(Integer, primary_key=True, autoincrement=True)
    robot_id = Column(String(64), nullable=False, index=True)
    target_type = Column(String(20), nullable=False)
    target_id = Column(String(64), nullable=False)
    list_mode = Column(String(20), default="none", nullable=False)
    capture_text = Column(Boolean, default=True, nullable=False)
    capture_image = Column(Boolean, default=True, nullable=False)
    capture_voice = Column(Boolean, default=True, nullable=False)
    capture_video = Column(Boolean, default=True, nullable=False)
    capture_file = Column(Boolean, default=False, nullable=False)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now, nullable=False)

    __table_args__ = (
        UniqueConstraint("robot_id", "target_type", "target_id", name="uq_capture_target_policy"),
        Index("idx_capture_policy_robot_mode", "robot_id", "list_mode"),
    )


class RoomProfile(Base):
    """Cached conversation metadata for local browsing."""

    __tablename__ = "room_profiles"

    room_id = Column(String(64), primary_key=True)
    platform = Column(String(20), nullable=False)
    display_name = Column(String(128), nullable=True)
    avatar_path = Column(String(255), nullable=True)
    avatar_file_hash = Column(String(64), nullable=True, index=True)
    avatar_source_url = Column(Text, nullable=True)
    avatar_status = Column(String(20), default="unknown", nullable=False)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now, nullable=False)


class UserProfile(Base):
    """Cached user metadata for local avatars and private chats."""

    __tablename__ = "user_profiles"

    user_id = Column(String(64), primary_key=True)
    platform = Column(String(20), nullable=False)
    display_name = Column(String(128), nullable=True)
    avatar_path = Column(String(255), nullable=True)
    avatar_file_hash = Column(String(64), nullable=True, index=True)
    avatar_source_url = Column(Text, nullable=True)
    avatar_status = Column(String(20), default="unknown", nullable=False)
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
    is_outgoing = Column(Boolean, nullable=True, index=True)
    raw_message = Column(Text, nullable=False)
    local_message = Column(Text, nullable=False)
    timestamp = Column(Integer, nullable=False, index=True)
    source_sequence = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=utc_now, nullable=False)

    __table_args__ = (
        Index("idx_room_timestamp", "room_id", "timestamp"),
        # Declared here as well as in migration 011: a database built straight
        # from the models used to lack it entirely.
        Index("idx_room_timestamp_sequence", "room_id", "timestamp", "source_sequence"),
        Index("idx_platform_room_timestamp", "platform", "room_id", "timestamp"),
        Index("idx_sender_timestamp", "sender_id", "timestamp"),
        Index("idx_message_type_timestamp", "message_type", "timestamp"),
    )


class RobotMessage(Base):
    """主视角关联表。"""

    __tablename__ = "robot_messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    robot_id = Column(String(64), nullable=False, index=True)
    msg_hash = Column(String(64), nullable=False, index=True)

    __table_args__ = (
        UniqueConstraint("robot_id", "msg_hash", name="uq_robot_message_view"),
        Index("idx_robot_message_robot_msg_hash", "robot_id", "msg_hash"),
    )


class MediaAsset(Base):
    """媒体资产索引表。"""

    __tablename__ = "media_assets"

    file_hash = Column(String(64), primary_key=True)
    content_sha256 = Column(String(64), nullable=True, unique=True, index=True)
    file_type = Column(String(20), nullable=False)
    file_size = Column(Integer, nullable=False)
    # Six services resolve assets by this column, including per-message media
    # resolution on every read, so it carries an index of its own.
    local_path = Column(String(255), nullable=False, index=True)
    created_at = Column(DateTime, default=utc_now, nullable=False)


class IdentityAlias(Base):
    """Maps protocol/source identifiers to one canonical QQ identity."""

    __tablename__ = "identity_aliases"

    id = Column(Integer, primary_key=True, autoincrement=True)
    platform = Column(String(20), nullable=False)
    identity_type = Column(String(20), nullable=False)
    canonical_id = Column(String(64), nullable=False, index=True)
    alias_id = Column(String(255), nullable=False)
    alias_type = Column(String(32), nullable=False)
    source_id = Column(String(64), ForeignKey("import_sources.id"), nullable=True, index=True)
    confidence = Column(String(20), default="observed", nullable=False)
    metadata_json = Column(Text, nullable=True)
    first_seen_at = Column(DateTime, default=utc_now, nullable=False)
    last_seen_at = Column(DateTime, default=utc_now, onupdate=utc_now, nullable=False, index=True)

    __table_args__ = (
        UniqueConstraint("platform", "identity_type", "alias_id", "source_id", name="uq_identity_alias_source"),
        Index("idx_identity_alias_canonical", "identity_type", "canonical_id"),
    )


class ProfileChangeRecord(Base):
    """Historical user/group profile changes without losing current profile state."""

    __tablename__ = "profile_change_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    identity_type = Column(String(20), nullable=False)
    identity_id = Column(String(64), nullable=False, index=True)
    old_display_name = Column(String(128), nullable=True)
    new_display_name = Column(String(128), nullable=True)
    old_avatar_file_hash = Column(String(64), nullable=True)
    new_avatar_file_hash = Column(String(64), nullable=True)
    source_id = Column(String(64), ForeignKey("import_sources.id"), nullable=True, index=True)
    observed_at = Column(DateTime, default=utc_now, nullable=False, index=True)


class MessagePart(Base):
    """Ordered protocol-neutral message segment."""

    __tablename__ = "message_parts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    msg_hash = Column(String(64), ForeignKey("messages.msg_hash"), nullable=False, index=True)
    ordinal = Column(Integer, nullable=False)
    part_type = Column(String(20), nullable=False, index=True)
    text_content = Column(Text, nullable=True)
    media_reference_id = Column(Integer, ForeignKey("message_media_references.id"), nullable=True, index=True)
    payload_json = Column(Text, nullable=True)
    source_format = Column(String(32), nullable=True)
    render_status = Column(String(20), default="parsed", nullable=False)

    __table_args__ = (
        UniqueConstraint("msg_hash", "ordinal", name="uq_message_part_ordinal"),
        Index("idx_message_parts_message_order", "msg_hash", "ordinal"),
    )


class ImportSource(Base):
    """A stable collector/source-device identity."""

    __tablename__ = "import_sources"

    id = Column(String(64), primary_key=True)
    source_type = Column(String(32), nullable=False)
    platform = Column(String(20), nullable=False)
    account_id = Column(String(64), nullable=False, index=True)
    device_id = Column(String(128), nullable=False)
    device_name = Column(String(128), nullable=True)
    qq_version = Column(String(64), nullable=True)
    schema_version = Column(String(64), nullable=True)
    status = Column(String(20), default="active", nullable=False, index=True)
    first_seen_at = Column(DateTime, default=utc_now, nullable=False)
    last_seen_at = Column(DateTime, default=utc_now, nullable=False, index=True)
    metadata_json = Column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("source_type", "account_id", "device_id", name="uq_import_source_device"),
        Index("idx_import_source_account_status", "account_id", "status"),
        CheckConstraint("status IN ('active','needs_key','disabled','error')", name="ck_import_source_status"),
    )


class ImportBatch(Base):
    """One initial, incremental, reconciliation, or media-rescan run."""

    __tablename__ = "import_batches"

    id = Column(String(64), primary_key=True)
    source_id = Column(String(64), ForeignKey("import_sources.id"), nullable=False, index=True)
    mode = Column(String(20), nullable=False)
    status = Column(String(20), default="running", nullable=False, index=True)
    started_at = Column(DateTime, default=utc_now, nullable=False, index=True)
    completed_at = Column(DateTime, nullable=True)
    scanned_messages = Column(Integer, default=0, nullable=False)
    inserted_messages = Column(Integer, default=0, nullable=False)
    updated_messages = Column(Integer, default=0, nullable=False)
    skipped_messages = Column(Integer, default=0, nullable=False)
    uploaded_media = Column(Integer, default=0, nullable=False)
    not_downloaded_media = Column(Integer, default=0, nullable=False)
    missing_media = Column(Integer, default=0, nullable=False)
    failed_media = Column(Integer, default=0, nullable=False)
    detail_json = Column(Text, nullable=True)

    __table_args__ = (
        Index("idx_import_batch_source_status_started", "source_id", "status", "started_at"),
        CheckConstraint("mode IN ('initial','incremental','reconcile','media_rescan')", name="ck_import_batch_mode"),
        CheckConstraint("status IN ('running','completed','partial','failed','cancelled')", name="ck_import_batch_status"),
        CheckConstraint(
            "scanned_messages >= 0 AND inserted_messages >= 0 AND updated_messages >= 0 AND skipped_messages >= 0 "
            "AND uploaded_media >= 0 AND not_downloaded_media >= 0 AND missing_media >= 0 AND failed_media >= 0",
            name="ck_import_batch_nonnegative_counts",
        ),
    )


class ImportBatchChunk(Base):
    """Cached response for an exact batch payload replay."""

    __tablename__ = "import_batch_chunks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    batch_id = Column(String(64), ForeignKey("import_batches.id"), nullable=False, index=True)
    request_hash = Column(String(64), nullable=False)
    response_json = Column(Text, nullable=False)
    created_at = Column(DateTime, default=utc_now, nullable=False)

    __table_args__ = (
        UniqueConstraint("batch_id", "request_hash", name="uq_import_batch_chunk_request"),
    )


class MessageSourceRecord(Base):
    """Replayable raw source record associated with a canonical message."""

    __tablename__ = "message_source_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    msg_hash = Column(String(64), ForeignKey("messages.msg_hash"), nullable=False, index=True)
    source_id = Column(String(64), ForeignKey("import_sources.id"), nullable=False, index=True)
    batch_id = Column(String(64), ForeignKey("import_batches.id"), nullable=True, index=True)
    source_table = Column(String(128), nullable=False)
    source_primary_key = Column(String(512), nullable=False)
    source_external_message_id = Column(String(64), nullable=True, index=True)
    platform_message_id = Column(String(64), nullable=True, index=True)
    schema_version = Column(String(64), nullable=True)
    raw_columns_json = Column(Text, nullable=True)
    raw_40800_protobuf = Column(Text, nullable=True)
    raw_40900_protobuf = Column(Text, nullable=True)
    metadata_json = Column(Text, nullable=True)
    imported_at = Column(DateTime, default=utc_now, nullable=False)
    last_seen_at = Column(DateTime, default=utc_now, nullable=False, index=True)

    __table_args__ = (
        UniqueConstraint("source_id", "source_table", "source_primary_key", name="uq_message_source_record"),
        Index("idx_message_source_msg_source", "msg_hash", "source_id"),
    )


class MessageMediaReference(Base):
    """Media metadata/state independent from a physically archived asset."""

    __tablename__ = "message_media_references"

    id = Column(Integer, primary_key=True, autoincrement=True)
    msg_hash = Column(String(64), ForeignKey("messages.msg_hash"), nullable=False, index=True)
    ordinal = Column(Integer, default=0, nullable=False)
    media_type = Column(String(20), nullable=False, index=True)
    source_state = Column(String(20), nullable=False, index=True)
    archive_state = Column(String(20), nullable=False, index=True)
    asset_file_hash = Column(String(64), ForeignKey("media_assets.file_hash"), nullable=True, index=True)
    thumbnail_file_hash = Column(String(64), ForeignKey("media_assets.file_hash"), nullable=True, index=True)
    file_name = Column(String(512), nullable=True)
    file_ext = Column(String(32), nullable=True)
    declared_file_size = Column(Integer, nullable=True)
    actual_file_size = Column(Integer, nullable=True)
    source_md5 = Column(String(128), nullable=True, index=True)
    source_sha1 = Column(String(128), nullable=True)
    source_uuid = Column(String(255), nullable=True, index=True)
    content_sha256 = Column(String(64), nullable=True, index=True)
    source_local_path = Column(Text, nullable=True)
    duration_ms = Column(Integer, nullable=True)
    width = Column(Integer, nullable=True)
    height = Column(Integer, nullable=True)
    failure_code = Column(String(64), nullable=True)
    failure_detail = Column(Text, nullable=True)
    metadata_json = Column(Text, nullable=True)
    first_seen_at = Column(DateTime, default=utc_now, nullable=False)
    last_checked_at = Column(DateTime, default=utc_now, nullable=False, index=True)
    archived_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("msg_hash", "ordinal", name="uq_message_media_ordinal"),
        Index("idx_message_media_state", "source_state", "archive_state"),
        CheckConstraint("ordinal >= 0", name="ck_message_media_ordinal"),
        CheckConstraint("source_state IN ('downloaded','not_downloaded','missing','unknown')", name="ck_message_media_source_state"),
        CheckConstraint("archive_state IN ('complete','thumbnail_only','metadata_only','failed')", name="ck_message_media_archive_state"),
        CheckConstraint(
            "(source_state = 'downloaded' AND archive_state IN ('complete','failed')) "
            "OR (source_state = 'not_downloaded' AND archive_state IN ('metadata_only','thumbnail_only')) "
            "OR (source_state = 'missing' AND archive_state IN ('metadata_only','thumbnail_only')) "
            "OR (source_state = 'unknown' AND archive_state = 'failed')",
            name="ck_message_media_state_combination",
        ),
        CheckConstraint(
            "(archive_state != 'complete' OR asset_file_hash IS NOT NULL) "
            "AND (archive_state != 'thumbnail_only' OR thumbnail_file_hash IS NOT NULL) "
            "AND (source_state != 'not_downloaded' OR asset_file_hash IS NULL)",
            name="ck_message_media_asset_requirements",
        ),
        CheckConstraint(
            "(declared_file_size IS NULL OR declared_file_size >= 0) "
            "AND (actual_file_size IS NULL OR actual_file_size >= 0) "
            "AND (duration_ms IS NULL OR duration_ms >= 0) "
            "AND (width IS NULL OR width >= 0) "
            "AND (height IS NULL OR height >= 0)",
            name="ck_message_media_nonnegative_metadata",
        ),
    )


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
    # API tokens are long-lived by design, so this stays NULL unless a lifetime
    # is configured; see ADMIN_TOKEN_TTL_DAYS.
    expires_at = Column(DateTime, nullable=True)


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
    # NULL on rows created before expiry existed; those fall back to
    # created_at plus the configured lifetime, so an old session cannot outlive
    # the policy simply by predating it.
    expires_at = Column(DateTime, nullable=True)


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
