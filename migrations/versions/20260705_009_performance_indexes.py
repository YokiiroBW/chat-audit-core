revision = "20260705_009"
down_revision = "20260705_008"
lightweight_version = "20260705_009_performance_indexes"
description = "Create performance indexes"


from alembic import op

from migrations.helpers import create_current_schema, has_index, has_table


def upgrade() -> None:
    create_current_schema()
    if has_table("messages"):
        if not has_index("messages", "idx_room_timestamp"):
            op.create_index("idx_room_timestamp", "messages", ["room_id", "timestamp"])
        if not has_index("messages", "idx_platform_room_timestamp"):
            op.create_index("idx_platform_room_timestamp", "messages", ["platform", "room_id", "timestamp"])
        if not has_index("messages", "idx_sender_timestamp"):
            op.create_index("idx_sender_timestamp", "messages", ["sender_id", "timestamp"])
        if not has_index("messages", "idx_message_type_timestamp"):
            op.create_index("idx_message_type_timestamp", "messages", ["message_type", "timestamp"])
    if has_table("robot_messages") and not has_index("robot_messages", "idx_robot_message_robot_msg_hash"):
        op.create_index("idx_robot_message_robot_msg_hash", "robot_messages", ["robot_id", "msg_hash"])


def downgrade() -> None:
    raise NotImplementedError("downgrade is not supported for production audit migrations")
