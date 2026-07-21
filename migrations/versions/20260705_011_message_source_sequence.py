"""Add source sequence ordering for QQNT messages."""

revision = "20260705_011"
down_revision = "20260705_010"
lightweight_version = "20260705_011_message_source_sequence"
description = "Add source message sequence ordering"

from alembic import op
from sqlalchemy import Column, Integer

from migrations.helpers import create_current_schema, has_column, has_index


def upgrade() -> None:
    create_current_schema()
    if not has_column("messages", "source_sequence"):
        op.add_column("messages", Column("source_sequence", Integer(), nullable=True))
    if not has_index("messages", "idx_room_timestamp_sequence"):
        op.create_index("idx_room_timestamp_sequence", "messages", ["room_id", "timestamp", "source_sequence"])


def downgrade() -> None:
    raise NotImplementedError("downgrade is not supported for production audit migrations")
