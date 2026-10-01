revision = "20260705_003"
down_revision = "20260705_002"
lightweight_version = "20260705_003_audit_logs"
description = "Create audit_logs table"


from migrations.helpers import create_current_schema


def upgrade() -> None:
    create_current_schema()


def downgrade() -> None:
    raise NotImplementedError("downgrade is not supported for production audit migrations")
