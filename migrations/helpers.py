from __future__ import annotations

from alembic import op
from sqlalchemy import inspect

from app.models import Base


def create_current_schema() -> None:
    Base.metadata.create_all(bind=op.get_bind())


def has_table(table_name: str) -> bool:
    return table_name in inspect(op.get_bind()).get_table_names()


def has_column(table_name: str, column_name: str) -> bool:
    if not has_table(table_name):
        return False
    return column_name in {column["name"] for column in inspect(op.get_bind()).get_columns(table_name)}


def has_index(table_name: str, index_name: str) -> bool:
    if not has_table(table_name):
        return False
    return index_name in {index["name"] for index in inspect(op.get_bind()).get_indexes(table_name)}
