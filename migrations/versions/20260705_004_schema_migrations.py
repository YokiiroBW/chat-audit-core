revision = "20260705_004_schema_migrations"
down_revision = "20260705_003_audit_logs"
description = "Create schema_migrations table"


def upgrade() -> None:
    """Created by SQLAlchemy metadata during startup."""


def downgrade() -> None:
    raise NotImplementedError("downgrade is not supported for production audit migrations")

