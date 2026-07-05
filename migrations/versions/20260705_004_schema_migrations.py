revision = "20260705_004_schema_migrations"
down_revision = "20260705_003_audit_logs"
description = "Create schema_migrations table"


from migrations.helpers import create_current_schema


def upgrade() -> None:
    create_current_schema()


def downgrade() -> None:
    raise NotImplementedError("downgrade is not supported for production audit migrations")
