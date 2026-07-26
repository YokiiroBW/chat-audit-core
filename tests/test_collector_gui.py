from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from collector.gui.app import build_parser
from collector.gui.controller import (
    SYNCABLE_COMPATIBILITY_STATUSES,
    CollectorGuiController,
    GuiSettings,
)
from collector.gui.tray import create_tray_image
from collector.gui.single_instance import AlreadyRunningError, SingleInstance
from collector.qqnt.sqlcipher import sqlcipher_available


class FakeCredentialStore:
    values_by_path: dict[str, dict[str, str]] = {}

    def __init__(self, path):
        self.path = str(path)
        self.values = self.values_by_path.setdefault(self.path, {})

    def set(self, name: str, value: str) -> None:
        self.values[name] = value
        path = Path(self.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.values), encoding="utf-8")

    def get(self, name: str):
        path = Path(self.path)
        if not self.values and path.is_file():
            self.values.update(json.loads(path.read_text(encoding="utf-8")))
        return self.values.get(name)


def _settings(data_root, *, token="operator-token", database_key="", collector_data_dir="", sync_interval_seconds=None, autostart=False):
    return GuiSettings(
        account_id="123456789",
        device_name="Test PC",
        data_root=str(data_root),
        server_url="http://collector.test",
        api_token=token,
        qqnt_database_key=database_key,
        collector_data_dir=str(collector_data_dir),
        sync_interval_minutes=5,
        sync_interval_seconds=sync_interval_seconds,
        autostart=autostart,
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
    assert controller.has_database_key() is False

    controller.save_settings(_settings(data_root, database_key="1234567890abcdef"))
    assert controller.has_database_key() is True
    assert "1234567890abcdef" not in config_path.read_text(encoding="utf-8")
    loaded = controller.load_settings()
    assert loaded is not None
    assert loaded.server_url == "http://collector.test"
    assert loaded.sync_interval_minutes == 5


def test_gui_saves_second_based_sync_interval(tmp_path):
    FakeCredentialStore.values_by_path.clear()
    data_root = tmp_path / "qqnt"
    (data_root / "nt_db").mkdir(parents=True)
    controller = CollectorGuiController(tmp_path / "collector.toml", credential_store_factory=FakeCredentialStore)

    config = controller.save_settings(_settings(data_root, sync_interval_seconds=30))

    assert config.collector.sync_interval_seconds == 30
    assert "sync_interval_seconds = 30" in (tmp_path / "collector.toml").read_text(encoding="utf-8")


def test_gui_persists_autostart_preference(tmp_path):
    FakeCredentialStore.values_by_path.clear()
    data_root = tmp_path / "qqnt"
    (data_root / "nt_db").mkdir(parents=True)
    controller = CollectorGuiController(tmp_path / "collector.toml", credential_store_factory=FakeCredentialStore)
    calls = []
    controller._set_autostart = lambda enabled: calls.append(enabled)

    config = controller.save_settings(_settings(data_root, autostart=True))

    assert config.collector.autostart is True
    assert calls == [True]
    assert "autostart = true" in (tmp_path / "collector.toml").read_text(encoding="utf-8")


def test_gui_migrates_collector_data_dir_without_losing_state_or_credentials(tmp_path):
    FakeCredentialStore.values_by_path.clear()
    data_root = tmp_path / "qqnt"
    (data_root / "nt_db").mkdir(parents=True)
    config_path = tmp_path / "config" / "collector.toml"
    old_dir = tmp_path / "collector-old"
    new_dir = tmp_path / "collector-new"
    controller = CollectorGuiController(config_path, credential_store_factory=FakeCredentialStore)

    controller.save_settings(_settings(data_root, collector_data_dir=old_dir))
    old_dir.joinpath("collector_state.sqlite3").write_bytes(b"state")
    old_dir.joinpath("collector-gui-test.lock").write_bytes(b"0")
    old_dir.joinpath("staging", "marker.bin").parent.mkdir(parents=True, exist_ok=True)
    old_dir.joinpath("staging", "marker.bin").write_bytes(b"media")
    new_dir.mkdir(parents=True)
    new_dir.joinpath("collector_state.sqlite3").write_bytes(b"partial")

    controller.save_settings(_settings(data_root, token="", collector_data_dir=new_dir))

    config = controller.load_settings()
    assert config is not None
    assert config.collector_data_dir == str(new_dir.resolve())
    assert controller.has_api_token() is True
    assert (new_dir / "collector_state.sqlite3").read_bytes() == b"state"
    assert not (new_dir / "collector-gui-test.lock").exists()
    assert (new_dir / "credentials.dpapi").is_file()
    assert (new_dir / "staging" / "marker.bin").read_bytes() == b"media"
    assert old_dir.is_dir()
    encoded_path = str(new_dir.resolve()).replace("\\", "\\\\")
    assert 'data_dir = "' + encoded_path + '"' in config_path.read_text(encoding="utf-8")


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

    assert report.status == "needs_key"
    assert report.qqnt_custom_count == 1
    assert "16" in report.message
    with pytest.raises(RuntimeError, match="16"):
        controller.start()


@pytest.mark.skipif(not sqlcipher_available(), reason="需要 sqlcipher3 才能构造加密库")
def test_gui_does_not_claim_encrypted_database_readable_before_it_is_opened(tmp_path):
    """托盘不得仅凭密钥长度就报"可读取"，但也不能因此永久禁用 QQNT 单来源同步。

    此前 `readable_count` 会把只通过密钥长度检查的加密库计入，托盘显示"ready / 可读取"，
    与 CODEX_HANDOFF.md 描述的安全边界不符。
    """
    import sqlcipher3.dbapi2 as sqlcipher

    from collector.qqnt.sqlcipher import QQNT_HEADER_SIZE, configure_connection

    FakeCredentialStore.values_by_path.clear()
    key = "0123456789abcdef"
    data_root = tmp_path / "qqnt"
    database_dir = data_root / "nt_db"
    database_dir.mkdir(parents=True)

    body = tmp_path / "body.db"
    connection = sqlcipher.connect(str(body))
    configure_connection(connection, key)
    connection.execute("CREATE TABLE group_msg_001 (msgId TEXT, msgSeq INTEGER)")
    connection.commit()
    connection.close()
    header = b"SQLite header 3\x00" + b"\x00" * 16 + b"QQ_NT DB" + b"\x00" * (QQNT_HEADER_SIZE - 40)
    (database_dir / "nt_msg.db").write_bytes(header + body.read_bytes())

    controller = CollectorGuiController(
        tmp_path / "collector.toml",
        credential_store_factory=FakeCredentialStore,
    )
    controller.save_settings(_settings(data_root, database_key=key))

    report = controller.inspect_databases()

    assert report.status == "needs_verification"
    assert report.readable_count == 0, "未实际打开就不能计入可读"
    assert report.qqnt_custom_count == 1
    assert "实测" in report.message
    # 仍须允许启动：可读性只能由扫描器真实打开剥头副本来证明，拦在这里等于废掉该部署形态。
    assert report.status in SYNCABLE_COMPATIBILITY_STATUSES


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


def test_gui_generates_unlock_script_with_safe_windows_paths(tmp_path):
    data_root = tmp_path / "QQ \u6570\u636e (\u6d4b\u8bd5)"
    install_dir = tmp_path / "Tencent QQ"
    data_root.mkdir()
    install_dir.mkdir()
    output = tmp_path / "\u684c\u9762" / "qq \u89e3\u9501.ps1"
    controller = CollectorGuiController(tmp_path / "collector.toml", credential_store_factory=FakeCredentialStore)

    generated = controller.generate_unlock_script(data_root, install_dir, output)

    assert generated == output.with_suffix(".bat").resolve()
    script = generated.with_suffix(".ps1").read_text(encoding="utf-8")
    launcher = generated.read_text(encoding="utf-8")
    assert "$qqDataDir = '" + str(data_root.resolve()).replace("'", "''") + "'" in script
    assert "$qqInstallDir = '" + str(install_dir.resolve()).replace("'", "''") + "'" in script
    assert "operator-token" not in script
    assert "qqnt_database_key" not in script
    assert "WrapperNodePath $wrapperNode" in script
    assert "powershell.exe" in launcher
    assert generated.with_suffix(".ps1").name in launcher


def test_gui_unlock_script_requires_both_directories(tmp_path):
    controller = CollectorGuiController(tmp_path / "collector.toml", credential_store_factory=FakeCredentialStore)
    with pytest.raises(ValueError, match="QQ"):
        controller.generate_unlock_script(tmp_path / "missing-data", tmp_path, tmp_path / "unlock.ps1")
    data_root = tmp_path / "data"
    data_root.mkdir()
    with pytest.raises(ValueError, match="QQ"):
        controller.generate_unlock_script(data_root, tmp_path / "missing-install", tmp_path / "unlock.ps1")


def test_transport_security_warnings_flag_plaintext_and_disabled_verification():
    # The collector carries an API token and archived chat content, so neither
    # plain HTTP off-box nor a disabled certificate check may pass silently.
    from collector.app.config import ServerSettings, transport_security_warnings

    assert transport_security_warnings(ServerSettings(base_url="https://audit.example.test")) == []
    # Loopback over HTTP is a legitimate local setup.
    assert transport_security_warnings(ServerSettings(base_url="http://127.0.0.1:8000")) == []
    assert transport_security_warnings(ServerSettings(base_url="http://localhost:8000")) == []

    plaintext = transport_security_warnings(ServerSettings(base_url="http://192.168.31.210:8000"))
    assert len(plaintext) == 1
    assert "HTTP" in plaintext[0]

    unverified = transport_security_warnings(ServerSettings(base_url="https://audit.example.test", verify_tls=False))
    assert len(unverified) == 1
    assert "verify_tls" in unverified[0]

    both = transport_security_warnings(ServerSettings(base_url="http://192.168.31.210:8000", verify_tls=False))
    assert len(both) == 2
