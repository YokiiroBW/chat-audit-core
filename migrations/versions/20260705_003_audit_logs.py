revision = "20260705_003_audit_logs"
down_revision = "20260705_002_message_external_message_id"
description = "Create audit_logs table"


def upgrade() -> None:
    """Created by SQLAlchemy metadata during startup."""


def downgrade() -> None:
    raise NotImplementedError("downgrade is not supported for production audit migrations")

