"""Add unified identity, message part, profile and media metadata."""

revision = "20260705_012"
down_revision = "20260705_011"
lightweight_version = "20260705_012_unified_archive_schema"
description = "Add unified identities, message parts, profile and media metadata"

from alembic import op
from sqlalchemy import Boolean, Column, String, Text

from migrations.helpers import create_current_schema, has_column, has_index


def upgrade() -> None:
    create_current_schema()
    for table_name, column in (
        ("messages", Column("is_outgoing", Boolean(), nullable=True)),
        ("media_assets", Column("content_sha256", String(64), nullable=True)),
        ("message_media_references", Column("content_sha256", String(64), nullable=True)),
        ("room_profiles", Column("avatar_file_hash", String(64), nullable=True)),
        ("room_profiles", Column("avatar_source_url", Text(), nullable=True)),
        ("room_profiles", Column("avatar_status", String(20), nullable=True)),
        ("user_profiles", Column("avatar_file_hash", String(64), nullable=True)),
        ("user_profiles", Column("avatar_source_url", Text(), nullable=True)),
        ("user_profiles", Column("avatar_status", String(20), nullable=True)),
    ):
        if not has_column(table_name, column.name):
            op.add_column(table_name, column)
    if not has_index("messages", "idx_messages_is_outgoing"):
        op.create_index("idx_messages_is_outgoing", "messages", ["is_outgoing"])
    if not has_index("media_assets", "idx_media_assets_content_sha256"):
        op.create_index("idx_media_assets_content_sha256", "media_assets", ["content_sha256"])


def downgrade() -> None:
    raise NotImplementedError("downgrade is not supported for production audit migrations")
