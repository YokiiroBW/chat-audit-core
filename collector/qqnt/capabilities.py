from __future__ import annotations

import importlib.util
import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class QQNTRuntimeCapabilities:
    sqlite_version: str
    sqlcipher_available: bool
    sqlcipher_provider: str | None
    nt_vfs_available: bool = False


def detect_runtime_capabilities() -> QQNTRuntimeCapabilities:
    """Report optional database runtimes without opening a QQNT database."""
    for module_name in ("pysqlcipher3", "sqlcipher3"):
        if importlib.util.find_spec(module_name) is not None:
            return QQNTRuntimeCapabilities(
                sqlite_version=sqlite3.sqlite_version,
                sqlcipher_available=True,
                sqlcipher_provider=module_name,
            )
    return QQNTRuntimeCapabilities(
        sqlite_version=sqlite3.sqlite_version,
        sqlcipher_available=False,
        sqlcipher_provider=None,
    )


__all__ = ["QQNTRuntimeCapabilities", "detect_runtime_capabilities"]
