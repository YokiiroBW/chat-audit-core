revision = "20260705_005_admin_tokens"
down_revision = "20260705_004_schema_migrations"
description = "Create admin_tokens table"


def upgrade() -> None:
    """Created by SQLAlchemy metadata during startup."""


def downgrade() -> None:
    raise NotImplementedError("downgrade is not supported for production audit migrations")

