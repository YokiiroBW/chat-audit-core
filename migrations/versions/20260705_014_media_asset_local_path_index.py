"""Index media assets by their public local path."""

revision = "20260705_014"
down_revision = "20260705_013"
lightweight_version = "20260705_014_media_asset_local_path_index"
description = "Index media assets by local path"

from alembic import op

from migrations.helpers import create_current_schema, has_index


def upgrade() -> None:
    create_current_schema()
    if not has_index("media_assets", "ix_media_assets_local_path"):
        op.create_index("ix_media_assets_local_path", "media_assets", ["local_path"])


def downgrade() -> None:
    raise NotImplementedError("downgrade is not supported for production audit migrations")
