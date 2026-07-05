import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from app.database import LIGHTWEIGHT_MIGRATION_REGISTRY


REPO_ROOT = Path(__file__).resolve().parents[1]


def _database_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.as_posix()}"


def _run_alembic_upgrade(db_path: Path) -> None:
    env = os.environ.copy()
    env["DATABASE_URL"] = _database_url(db_path)
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT,
        env=env,
        check=True,
        text=True,
        capture_output=True,
    )


def _columns(db_path: Path, table: str) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def test_alembic_upgrade_head_initializes_current_schema(tmp_path):
    db_path = tmp_path / "alembic.sqlite3"

    _run_alembic_upgrade(db_path)

    with sqlite3.connect(db_path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]

    assert {
        "adapters",
        "messages",
        "audit_logs",
        "schema_migrations",
        "admin_tokens",
        "admin_users",
        "admin_sessions",
        "system_settings",
        "capture_target_policies",
        "alembic_version",
    } <= tables
    assert version == "20260705_008"


def test_alembic_upgrade_head_adds_legacy_compatibility_columns(tmp_path):
    db_path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE adapters (
                id VARCHAR(64) NOT NULL PRIMARY KEY,
                platform VARCHAR(20) NOT NULL,
                config_json TEXT,
                status VARCHAR(20) NOT NULL,
                updated_at DATETIME NOT NULL
            );
            CREATE TABLE messages (
                msg_hash VARCHAR(64) NOT NULL PRIMARY KEY,
                platform VARCHAR(20) NOT NULL,
                room_id VARCHAR(64) NOT NULL,
                message_type VARCHAR(20) NOT NULL,
                sender_id VARCHAR(64) NOT NULL,
                nickname VARCHAR(128),
                raw_message TEXT NOT NULL,
                local_message TEXT NOT NULL,
                timestamp INTEGER NOT NULL,
                created_at DATETIME NOT NULL
            );
            """
        )

    _run_alembic_upgrade(db_path)

    assert "current_robot_id" in _columns(db_path, "adapters")
    assert "external_message_id" in _columns(db_path, "messages")
