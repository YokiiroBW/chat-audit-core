revision = "20260705_003_audit_logs"
down_revision = "20260705_002_message_external_message_id"
description = "Create audit_logs table"


from migrations.helpers import create_current_schema


def upgrade() -> None:
    create_current_schema()


def downgrade() -> None:
    raise NotImplementedError("downgrade is not supported for production audit migrations")
