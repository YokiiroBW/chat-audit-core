revision = "20260705_007_admin_users_sessions"
down_revision = "20260705_006_system_settings"
description = "Create admin_users and admin_sessions tables"


def upgrade() -> None:
    """Created by SQLAlchemy metadata during startup."""


def downgrade() -> None:
    raise NotImplementedError("downgrade is not supported for production audit migrations")

