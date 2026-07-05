revision = "20260705_006_system_settings"
down_revision = "20260705_005_admin_tokens"
description = "Create system_settings table"


def upgrade() -> None:
    """Created by SQLAlchemy metadata during startup."""


def downgrade() -> None:
    raise NotImplementedError("downgrade is not supported for production audit migrations")

