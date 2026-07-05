revision = "20260705_002_message_external_message_id"
down_revision = "20260705_001_adapter_current_robot_id"
description = "Add messages.external_message_id"


def upgrade() -> None:
    """Mirrors the lightweight startup migration."""


def downgrade() -> None:
    raise NotImplementedError("downgrade is not supported for production audit migrations")

