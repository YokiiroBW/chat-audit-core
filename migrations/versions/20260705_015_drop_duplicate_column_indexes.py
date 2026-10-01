"""Drop single-column indexes duplicated under a second name."""

revision = "20260705_015"
down_revision = "20260705_014"
lightweight_version = "20260705_015_drop_duplicate_column_indexes"
description = "Drop duplicate single-column indexes"

from alembic import op

from migrations.helpers import create_current_schema, has_index


def upgrade() -> None:
    create_current_schema()
    for table_name, index_name in (
        ("media_assets", "idx_media_assets_content_sha256"),
        ("messages", "idx_messages_is_outgoing"),
    ):
        if has_index(table_name, index_name):
            op.drop_index(index_name, table_name=table_name)


def downgrade() -> None:
    raise NotImplementedError("downgrade is not supported for production audit migrations")
