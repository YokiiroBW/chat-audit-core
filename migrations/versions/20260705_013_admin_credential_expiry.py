"""Add expiry to admin sessions and API tokens."""

revision = "20260705_013"
down_revision = "20260705_012"
lightweight_version = "20260705_013_admin_credential_expiry"
description = "Add admin session and token expiry"

from alembic import op
from sqlalchemy import Column, DateTime

from migrations.helpers import create_current_schema, has_column


def upgrade() -> None:
    create_current_schema()
    for table_name in ("admin_sessions", "admin_tokens"):
        if not has_column(table_name, "expires_at"):
            op.add_column(table_name, Column("expires_at", DateTime(), nullable=True))


def downgrade() -> None:
    raise NotImplementedError("downgrade is not supported for production audit migrations")
