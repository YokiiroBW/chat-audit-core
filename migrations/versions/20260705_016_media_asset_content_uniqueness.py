"""Merge duplicate media assets and backfill avatar status."""

revision = "20260705_016"
down_revision = "20260705_015"
lightweight_version = "20260705_016_media_asset_content_uniqueness"
description = "Merge duplicate media assets and backfill avatar status"

from alembic import op

from app.database import merge_duplicate_media_assets_sync
from migrations.helpers import create_current_schema


def upgrade() -> None:
    create_current_schema()
    # The same routine the lightweight registry runs. Sharing it matters here:
    # this migration rewrites and deletes rows, so two implementations could
    # disagree about which one survives.
    merge_duplicate_media_assets_sync(op.get_bind().exec_driver_sql)


def downgrade() -> None:
    raise NotImplementedError("downgrade is not supported for production audit migrations")
