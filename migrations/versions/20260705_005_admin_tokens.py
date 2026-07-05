revision = "20260705_005_admin_tokens"
down_revision = "20260705_004_schema_migrations"
description = "Create admin_tokens table"


from migrations.helpers import create_current_schema


def upgrade() -> None:
    create_current_schema()


def downgrade() -> None:
    raise NotImplementedError("downgrade is not supported for production audit migrations")
