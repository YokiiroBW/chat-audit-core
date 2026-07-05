revision = "20260705_006"
down_revision = "20260705_005"
lightweight_version = "20260705_006_system_settings"
description = "Create system_settings table"


from migrations.helpers import create_current_schema


def upgrade() -> None:
    create_current_schema()


def downgrade() -> None:
    raise NotImplementedError("downgrade is not supported for production audit migrations")
