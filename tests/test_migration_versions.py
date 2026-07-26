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
