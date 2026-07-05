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
