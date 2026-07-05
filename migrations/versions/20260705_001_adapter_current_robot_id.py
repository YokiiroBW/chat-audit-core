revision = "20260705_001"
down_revision = None
lightweight_version = "20260705_001_adapter_current_robot_id"
description = "Add adapters.current_robot_id"


from alembic import op
import sqlalchemy as sa

from migrations.helpers import create_current_schema, has_column, has_index, has_table


def upgrade() -> None:
    create_current_schema()
    if has_table("adapters") and not has_column("adapters", "current_robot_id"):
        op.add_column("adapters", sa.Column("current_robot_id", sa.String(length=64), nullable=True))
    if has_table("adapters") and has_column("adapters", "current_robot_id") and not has_index("adapters", "ix_adapters_current_robot_id"):
        op.create_index("ix_adapters_current_robot_id", "adapters", ["current_robot_id"])


def downgrade() -> None:
    raise NotImplementedError("downgrade is not supported for production audit migrations")
