import importlib.util
from pathlib import Path

from app.database import LIGHTWEIGHT_MIGRATION_REGISTRY


def _load_version_module(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_version_scripts_mirror_lightweight_migration_registry():
    version_dir = Path("migrations/versions")
    modules = [_load_version_module(path) for path in sorted(version_dir.glob("*.py")) if path.name != "__init__.py"]
    registry_versions = [migration.version for migration in LIGHTWEIGHT_MIGRATION_REGISTRY]

    expected_revisions = [f"20260705_{index:03d}" for index in range(1, len(registry_versions) + 1)]
    assert [module.revision for module in modules] == expected_revisions
    assert [module.lightweight_version for module in modules] == registry_versions
    assert [module.description for module in modules] == [migration.description for migration in LIGHTWEIGHT_MIGRATION_REGISTRY]

    expected_down_revisions = [None, *expected_revisions[:-1]]
    assert [module.down_revision for module in modules] == expected_down_revisions
    assert all(callable(module.upgrade) for module in modules)
    assert all(callable(module.downgrade) for module in modules)


def test_a_migrated_database_matches_one_built_from_the_models():
    # Migrations and models drifted apart unnoticed: four indexes existed only
    # after migrating, three of them shadowing an index the models already
    # declare on the same column under a different prefix, so every write
    # maintained two identical trees.
    import asyncio
    import sqlite3
    import tempfile

    from sqlalchemy.ext.asyncio import create_async_engine

    from app.models import Base

    async def build(path: Path, *, migrate: bool) -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
                if migrate:
                    for migration in LIGHTWEIGHT_MIGRATION_REGISTRY:
                        await migration.apply(connection)
        finally:
            await engine.dispose()

    def describe(path: Path) -> dict:
        connection = sqlite3.connect(path)
        try:
            tables = sorted(
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            )
            return {
                "tables": tables,
                "indexes": sorted(
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"
                    )
                ),
                "columns": {
                    table: sorted(row[1] for row in connection.execute(f"PRAGMA table_info({table})"))
                    for table in tables
                },
            }
        finally:
            connection.close()

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        asyncio.run(build(root / "fresh.sqlite3", migrate=False))
        asyncio.run(build(root / "migrated.sqlite3", migrate=True))
        fresh = describe(root / "fresh.sqlite3")
        migrated = describe(root / "migrated.sqlite3")

    assert migrated["tables"] == fresh["tables"]
    assert migrated["columns"] == fresh["columns"]
    assert migrated["indexes"] == fresh["indexes"]


def test_duplicate_media_assets_are_merged_not_dropped():
    # Deleting a duplicate row on its own would leave every message that pointed
    # at it without media. References have to move to the surviving row first.
    import asyncio
    import sqlite3
    import tempfile

    from sqlalchemy.ext.asyncio import create_async_engine

    from app.database import _merge_duplicate_media_assets
    from app.models import Base

    async def run(path: Path) -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
                # Drop the unique index so duplicates can be planted, the way an
                # archive written before content_sha256 existed would look.
                await connection.exec_driver_sql("DROP INDEX IF EXISTS ix_media_assets_content_sha256")
                for file_hash, created_at in (("keeper-hash", "2026-01-01"), ("loser-hash", "2026-06-01")):
                    await connection.exec_driver_sql(
                        "INSERT INTO media_assets (file_hash, content_sha256, file_type, file_size, local_path, created_at)"
                        f" VALUES ('{file_hash}', 'shared-sha', 'image', 10, '/media/{file_hash}.jpg', '{created_at}')"
                    )
                await connection.exec_driver_sql(
                    "INSERT INTO message_media_references"
                    " (msg_hash, ordinal, media_type, source_state, archive_state, asset_file_hash, first_seen_at, last_checked_at)"
                    " VALUES ('m1', 0, 'image', 'downloaded', 'complete', 'loser-hash', '2026-01-01', '2026-01-01')"
                )
                # Rebuild user_profiles the way it looked before avatar_status
                # became NOT NULL; the current schema will not accept the NULL a
                # legacy archive actually holds.
                await connection.exec_driver_sql("DROP TABLE user_profiles")
                await connection.exec_driver_sql(
                    "CREATE TABLE user_profiles ("
                    " user_id VARCHAR(64) PRIMARY KEY, platform VARCHAR(20) NOT NULL,"
                    " display_name VARCHAR(128), avatar_path VARCHAR(255), avatar_file_hash VARCHAR(64),"
                    " avatar_source_url TEXT, avatar_status VARCHAR(20), updated_at DATETIME NOT NULL)"
                )
                await connection.exec_driver_sql(
                    "INSERT INTO user_profiles (user_id, platform, avatar_file_hash, avatar_status, updated_at)"
                    " VALUES ('u1', 'qq', 'loser-hash', NULL, '2026-01-01')"
                )
                await _merge_duplicate_media_assets(connection)
        finally:
            await engine.dispose()

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "legacy.sqlite3"
        asyncio.run(run(path))
        connection = sqlite3.connect(path)
        try:
            assets = [row[0] for row in connection.execute("SELECT file_hash FROM media_assets")]
            reference = connection.execute("SELECT asset_file_hash FROM message_media_references").fetchone()[0]
            profile = connection.execute("SELECT avatar_file_hash, avatar_status FROM user_profiles").fetchone()
            indexes = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        finally:
            connection.close()

    # The oldest row survives and the newer duplicate is gone.
    assert assets == ["keeper-hash"]
    # Both references followed it rather than dangling.
    assert reference == "keeper-hash"
    assert profile[0] == "keeper-hash"
    # A NULL avatar_status is neither cached nor pending, so nothing picks it up.
    assert profile[1] == "unknown"
    assert "ix_media_assets_content_sha256" in indexes
