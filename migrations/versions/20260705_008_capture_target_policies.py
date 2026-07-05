revision = "20260705_008"
down_revision = "20260705_007"
lightweight_version = "20260705_008_capture_target_policies"
description = "Create capture_target_policies table"


from migrations.helpers import create_current_schema


def upgrade() -> None:
    create_current_schema()


def downgrade() -> None:
    raise NotImplementedError("downgrade is not supported for production audit migrations")
