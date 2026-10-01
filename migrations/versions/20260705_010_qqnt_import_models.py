revision = "20260705_010"
down_revision = "20260705_009"
lightweight_version = "20260705_010_qqnt_import_models"
description = "Create QQNT import and media reference tables"


from migrations.helpers import create_current_schema


def upgrade() -> None:
    create_current_schema()


def downgrade() -> None:
    raise NotImplementedError("downgrade is not supported for production audit migrations")
