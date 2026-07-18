from __future__ import annotations

import sqlite3

import pytest

from collector.gui.app import build_parser
from collector.gui.controller import CollectorGuiController, GuiSettings
from collector.gui.tray import create_tray_image
from collector.gui.single_instance import AlreadyRunningError, SingleInstance


class FakeCredentialStore:
    values_by_path: dict[str, dict[str, str]] = {}

    def __init__(self, path):
        self.path = str(path)
        self.values = self.values_by_path.setdefault(self.path, {})

    def set(self, name: str, value: str) -> None:
        self.values[name] = value

    def get(self, name: str):
        return self.values.get(name)


def _settings(data_root, *, token="operator-token"):
    return GuiSettings(
        account_id="123456789",
        device_name="Test PC",
        data_root=str(data_root),
        server_url="http://collector.test",
        api_token=token,
        sync_interval_minutes=5,
        verify_tls=False,
    )


def _plain_message_database(path):
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE group_msg_001 (
                msgId TEXT NOT NULL, msgSeq INTEGER NOT NULL, msgTime INTEGER NOT NULL,
                peerUid TEXT NOT NULL, senderUid TEXT NOT NULL, msgContent TEXT NOT NULL,
                chatType TEXT NOT NULL
            )
            """
        )


def test_gui_settings_write_toml_without_plaintext_token(tmp_path):
    FakeCredentialStore.values_by_path.clear()
    data_root = tmp_path / "qqnt"
    (data_root / "nt_db").mkdir(parents=True)
    config_path = tmp_path / "collector.toml"
    controller = CollectorGuiController(config_path, credential_store_factory=FakeCredentialStore)

    config = controller.save_settings(_settings(data_root))

    config_text = config_path.read_text(encoding="utf-8")
    assert "operator-token" not in config_text
    assert config.collector.account_id == "123456789"
    assert config.qq.read_mode == "snapshot_copy"
    assert config.paths.root == tmp_path.resolve()
    assert controller.has_api_token() is True
    loaded = controller.load_settings()
    assert loaded is not None
    assert loaded.server_url == "http://collector.test"
    assert loaded.sync_interval_minutes == 5


def test_gui_reports_real_qqnt_header_as_incompatible(tmp_path):
    FakeCredentialStore.values_by_path.clear()
    data_root = tmp_path / "qqnt"
    database_dir = data_root / "nt_db"
    database_dir.mkdir(parents=True)
    (database_dir / "nt_msg.db").write_bytes(
        b"SQLite header 3\x00" + b"\x00" * 16 + b"QQ_NT DB" + b"\x00" * 32
    )
    controller = CollectorGuiController(
        tmp_path / "collector.toml",
        credential_store_factory=FakeCredentialStore,
    )
    controller.save_settings(_settings(data_root))

    report = controller.inspect_databases()

    assert report.status == "unsupported"
    assert report.qqnt_custom_count == 1
    assert "NT VFS" in report.message
    with pytest.raises(RuntimeError, match="NT VFS"):
        controller.start()


def test_gui_reports_plain_fixture_database_as_ready(tmp_path):
    FakeCredentialStore.values_by_path.clear()
    data_root = tmp_path / "qqnt"
    database_dir = data_root / "nt_db"
    database_dir.mkdir(parents=True)
    _plain_message_database(database_dir / "nt_msg.db")
    controller = CollectorGuiController(
        tmp_path / "collector.toml",
        credential_store_factory=FakeCredentialStore,
    )
    controller.save_settings(_settings(data_root))

    report = controller.inspect_databases()
    snapshot = controller.status_snapshot()

    assert report.status == "ready"
    assert report.readable_count == 1
    assert snapshot["configured"] is True
    assert snapshot["has_api_token"] is True
    assert snapshot["running"] is False


def test_tray_icon_and_gui_arguments_are_available(tmp_path):
    image = create_tray_image(32)
    args = build_parser().parse_args(["--config", str(tmp_path / "collector.toml"), "--start-hidden"])

    assert image.size == (32, 32)
    assert image.getbbox() is not None
    assert args.start_hidden is True
    assert args.config.endswith("collector.toml")


def test_gui_single_instance_lock_rejects_duplicate_process(tmp_path):
    first = SingleInstance(tmp_path / "collector.toml")
    second = SingleInstance(tmp_path / "collector.toml")
    first.acquire()
    try:
        with pytest.raises(AlreadyRunningError):
            second.acquire()
    finally:
        first.release()
