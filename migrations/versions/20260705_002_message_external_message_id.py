revision = "20260705_002_message_external_message_id"
down_revision = "20260705_001_adapter_current_robot_id"
description = "Add messages.external_message_id"


from alembic import op
import sqlalchemy as sa

from migrations.helpers import create_current_schema, has_column, has_index, has_table


def upgrade() -> None:
    create_current_schema()
    if has_table("messages") and not has_column("messages", "external_message_id"):
        op.add_column("messages", sa.Column("external_message_id", sa.String(length=64), nullable=True))
    if has_table("messages") and has_column("messages", "external_message_id") and not has_index("messages", "ix_messages_external_message_id"):
        op.create_index("ix_messages_external_message_id", "messages", ["external_message_id"])


def downgrade() -> None:
    raise NotImplementedError("downgrade is not supported for production audit migrations")
