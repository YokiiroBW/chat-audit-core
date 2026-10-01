from collector.qqnt.versions.base import SchemaAdapterError
from collector.qqnt.versions.fallback import FallbackSQLiteAdapter
from collector.qqnt.versions.qqnt_9_9_x import QQNT99xAdapter


def select_adapter(report):
    for adapter in (QQNT99xAdapter(), FallbackSQLiteAdapter()):
        if adapter.supports(report):
            return adapter
    raise SchemaAdapterError("DB_SCHEMA_UNSUPPORTED", "no readable message tables were detected")


__all__ = ["FallbackSQLiteAdapter", "QQNT99xAdapter", "SchemaAdapterError", "select_adapter"]
