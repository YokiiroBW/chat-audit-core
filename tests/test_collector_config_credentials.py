from __future__ import annotations

import os
import json
import logging
import zipfile
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from collector.app.config import CollectorConfigError, load_config
from collector.app.credential_store import CredentialStore, WindowsDpapiProtector
from collector.app.diagnostics import create_diagnostic_bundle
from collector.app.logging import configure_logging
from collector.app.main import main
from collector.sync.state import CollectorStateStore


def _write_config(path: Path, root: Path, *, extra: str = "") -> None:
    path.write_text(
        f"""
[collector]
account_id = "123456789"
device_id = "test-device"
device_name = "Test PC"
data_dir = "{str(root).replace('\\', '\\\\')}"
sync_interval_minutes = 1
reconcile_interval_hours = 1
staging_max_gb = 1
initial_import_batch_size = 20
incremental_batch_size = 10

[qq]
read_mode = "direct_readonly"
enable_vss_fallback = true

[server]
base_url = "http://collector.test"
request_timeout_seconds = 10
verify_tls = false

[media]
copy_before_upload = true
validate_video_with_ffprobe = false
upload_thumbnails = true
rescan_not_downloaded_days = 30
{extra}
""".strip(),
        encoding="utf-8",
    )


def test_collector_config_loads_paths_and_rejects_plaintext_secrets(tmp_path):
    config_path = tmp_path / "collector.toml"
    root = tmp_path / "collector-data"
    _write_config(config_path, root)

    config = load_config(config_path)

    assert config.collector.account_id == "123456789"
    assert config.collector.device_id == "test-device"
    assert config.server.base_url == "http://collector.test"
    assert config.paths.state_db == root.resolve() / "collector_state.sqlite3"

    _write_config(config_path, root, extra='api_token = "plaintext-secret"')
    with pytest.raises(CollectorConfigError, match="sensitive field"):
        load_config(config_path)


class ReverseProtector:
    def protect(self, plaintext: bytes) -> bytes:
        return plaintext[::-1]

    def unprotect(self, ciphertext: bytes) -> bytes:
        return ciphertext[::-1]


def test_credential_store_never_writes_plaintext(tmp_path):
    path = tmp_path / "credentials.dpapi"
    store = CredentialStore(path, protector=ReverseProtector())

    store.set("api_token", "super-secret-token")
    store.set("qqnt_database_key", "database-secret")

    assert store.get("api_token") == "super-secret-token"
    assert store.list_names() == ["api_token", "qqnt_database_key"]
    assert b"super-secret-token" not in path.read_bytes()
    assert b"database-secret" not in path.read_bytes()
    assert store.delete("api_token") is True
    assert store.get("api_token") is None


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI test")
def test_windows_dpapi_roundtrip(tmp_path):
    path = tmp_path / "credentials.dpapi"
    store = CredentialStore(path, protector=WindowsDpapiProtector())

    store.set("api_token", "dpapi-secret")

    assert store.get("api_token") == "dpapi-secret"
    assert b"dpapi-secret" not in path.read_bytes()


def test_collector_cli_init_simulate_and_status(tmp_path, capsys):
    initialized = tmp_path / "initialized.toml"
    assert main(["--config", str(initialized), "init"]) == 0
    assert initialized.exists()

    config_path = tmp_path / "collector.toml"
    root = tmp_path / "collector-data"
    _write_config(config_path, root)
    assert main(["--config", str(config_path), "simulate", "--message-id", "cli-demo"]) == 0
    assert main(["--config", str(config_path), "status", "--json"]) == 0

    output = capsys.readouterr().out
    assert "Queued simulated message" in output
    assert '"pending": 1' in output


def test_logging_rotates_and_diagnostic_bundle_excludes_credentials(tmp_path):
    config_path = tmp_path / "collector.toml"
    root = tmp_path / "collector-data"
    _write_config(config_path, root)
    config = load_config(config_path)
    config.paths.ensure()
    store = CollectorStateStore(config.paths.state_db)
    store.initialize()
    CredentialStore(config.paths.credential_store, protector=ReverseProtector()).set(
        "api_token",
        "super-secret-token",
    )
    configure_logging(config.paths.log_dir)
    logger = logging.getLogger("collector.test")
    logger.warning("Authorization: Bearer super-secret-token C:\\Users\\Administrator\\private")

    handlers = logging.getLogger("collector").handlers
    rotating = next(handler for handler in handlers if isinstance(handler, RotatingFileHandler))
    assert rotating.maxBytes == 10 * 1024 * 1024
    assert rotating.backupCount == 5

    bundle = create_diagnostic_bundle(config, store, output_path=tmp_path / "diagnostics.zip")
    with zipfile.ZipFile(bundle) as archive:
        names = set(archive.namelist())
        combined = b"".join(archive.read(name) for name in names)
        manifest = json.loads(archive.read("manifest.json"))

    assert names == {"manifest.json", "status.json", "collector.log.tail.jsonl"}
    assert b"super-secret-token" not in combined
    assert "credentials.dpapi" not in names
    assert manifest["account_fingerprint"] != config.collector.account_id
    assert "credentials.dpapi" in manifest["excluded"]
