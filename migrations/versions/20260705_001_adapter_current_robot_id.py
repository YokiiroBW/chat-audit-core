revision = "20260705_001_adapter_current_robot_id"
down_revision = None
description = "Add adapters.current_robot_id"


def upgrade() -> None:
    """Mirrors the lightweight startup migration."""


def downgrade() -> None:
    raise NotImplementedError("downgrade is not supported for production audit migrations")

