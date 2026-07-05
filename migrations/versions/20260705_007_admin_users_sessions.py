revision = "20260705_007_admin_users_sessions"
down_revision = "20260705_006_system_settings"
description = "Create admin_users and admin_sessions tables"


from migrations.helpers import create_current_schema


def upgrade() -> None:
    create_current_schema()


def downgrade() -> None:
    raise NotImplementedError("downgrade is not supported for production audit migrations")
