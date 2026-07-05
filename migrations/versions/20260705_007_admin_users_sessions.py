revision = "20260705_007"
down_revision = "20260705_006"
lightweight_version = "20260705_007_admin_users_sessions"
description = "Create admin_users and admin_sessions tables"


from migrations.helpers import create_current_schema


def upgrade() -> None:
    create_current_schema()


def downgrade() -> None:
    raise NotImplementedError("downgrade is not supported for production audit migrations")
