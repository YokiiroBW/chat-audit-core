revision = "20260705_004"
down_revision = "20260705_003"
lightweight_version = "20260705_004_schema_migrations"
description = "Create schema_migrations table"


from migrations.helpers import create_current_schema


def upgrade() -> None:
    create_current_schema()


def downgrade() -> None:
    raise NotImplementedError("downgrade is not supported for production audit migrations")
