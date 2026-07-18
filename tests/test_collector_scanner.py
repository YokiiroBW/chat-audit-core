from __future__ import annotations

import json
import sqlite3

from collector.app.config import load_config
from collector.app.main import main
from collector.media.indexer import MediaFileIndex
from collector.sync.queue import CollectorQueue
from collector.sync.scanner import QQNTScanner
from collector.sync.state import CollectorStateStore


def _create_database(path):
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE group_msg_001 (
                msgId TEXT NOT NULL,
                msgSeq INTEGER NOT NULL,
                msgTime INTEGER NOT NULL,
                peerUid TEXT NOT NULL,
                senderUid TEXT NOT NULL,
                msgContent TEXT NOT NULL,
                chatType TEXT NOT NULL
            )
            """
        )
        connection.executemany(
            "INSERT INTO group_msg_001 VALUES(?,?,?,?,?,?,?)",
            [
                ("1", 1, 1000, "group-a", "user-a", '{"elements":[{"type":"text","text":"first"}]}', "group"),
                ("2", 2, 1000, "group-a", "user-b", '{"elements":[{"type":"text","text":"second"}]}', "group"),
                ("3", 3, 1010, "group-a", "user-c", '{"elements":[{"type":"text","text":"third"}]}', "group"),
            ],
        )


def _create_large_database(path, count):
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE group_msg_001 (
                msgId INTEGER NOT NULL, msgSeq INTEGER NOT NULL, msgTime INTEGER NOT NULL,
                peerUid TEXT NOT NULL, senderUid TEXT NOT NULL, msgContent TEXT NOT NULL,
                chatType TEXT NOT NULL
            )
            """
        )
        connection.executemany(
            "INSERT INTO group_msg_001 VALUES(?,?,?,?,?,?,?)",
            (
                (
                    index,
                    index,
                    1783000000 + index,
                    "group-a",
                    f"user-{index % 10}",
                    f'{{"elements":[{{"type":"text","text":"message-{index}"}}]}}',
                    "group",
                )
                for index in range(1, count + 1)
            ),
        )


def _write_config(path, collector_root, data_root):
    root_text = str(collector_root).replace("\\", "\\\\")
    data_root_text = str(data_root).replace("\\", "\\\\")
    path.write_text(
        f"""
[collector]
account_id = "123456789"
device_id = "test-device"
device_name = "Test PC"
data_dir = "{root_text}"
initial_import_batch_size = 2
incremental_batch_size = 2
staging_max_gb = 1

[qq]
data_root = "{data_root_text}"
read_mode = "direct_readonly"
enable_vss_fallback = false

[server]
base_url = "http://collector.test"
verify_tls = false

[media]
copy_before_upload = true
validate_video_with_ffprobe = false
upload_thumbnails = true
rescan_not_downloaded_days = 30
""".strip(),
        encoding="utf-8",
    )
    return load_config(path)


def _runtime(config):
    store = CollectorStateStore(config.paths.state_db)
    store.initialize()
    source_id = "source"
    store.upsert_source(
        source_id=source_id,
        source_type="qqnt_local_db",
        account_id=config.collector.account_id,
        device_id=config.collector.device_id,
    )
    return store, CollectorQueue(store), source_id


def test_scanner_pages_messages_enqueues_them_and_commits_cursor(tmp_path):
    data_root = tmp_path / "qqnt"
    database_dir = data_root / "database"
    database_dir.mkdir(parents=True)
    database_path = database_dir / "nt_msg.db"
    _create_database(database_path)
    config = _write_config(tmp_path / "collector.toml", tmp_path / "collector-data", data_root)
    store, queue, source_id = _runtime(config)
    scanner = QQNTScanner(config, store, queue, source_id=source_id)

    initial = scanner.scan(mode="initial")

    assert initial.scanned_databases == 1
    assert initial.scanned_tables == 1
    assert initial.scanned_messages == 3
    assert initial.parser_failures == 0
    assert initial.issues == []
    assert queue.counts()["messages"] == {"pending": 3}
    with store.connect() as connection:
        cursor = json.loads(connection.execute("SELECT cursor_json FROM table_cursors").fetchone()["cursor_json"])
    assert cursor == {
        "last_msg_id": "3",
        "last_msg_seq": 3,
        "last_msg_time": 1010,
        "last_row_identity": "3",
    }

    repeated = scanner.scan(mode="incremental")

    assert repeated.scanned_messages == 3
    assert queue.counts()["messages"] == {"pending": 3}


def test_scan_cli_discovers_database_and_reports_json(tmp_path, capsys):
    data_root = tmp_path / "qqnt"
    database_dir = data_root / "database"
    database_dir.mkdir(parents=True)
    _create_database(database_dir / "nt_msg.db")
    config_path = tmp_path / "collector.toml"
    _write_config(config_path, tmp_path / "collector-data", data_root)

    exit_code = main(["--config", str(config_path), "scan", "--mode", "initial", "--json"])

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output["scanned_messages"] == 3
    assert output["partial"] is False


def test_scan_without_discovered_databases_is_an_error(tmp_path, capsys):
    data_root = tmp_path / "empty-qqnt"
    data_root.mkdir()
    config_path = tmp_path / "collector.toml"
    _write_config(config_path, tmp_path / "collector-data", data_root)

    assert main(["--config", str(config_path), "scan"]) == 2
    assert "no QQNT message databases discovered" in capsys.readouterr().err


def test_initial_scan_resumes_after_process_restart_and_honors_limit(tmp_path):
    data_root = tmp_path / "qqnt"
    database_dir = data_root / "database"
    database_dir.mkdir(parents=True)
    database_path = database_dir / "nt_msg.db"
    _create_large_database(database_path, 7)
    config = _write_config(tmp_path / "collector.toml", tmp_path / "collector-data", data_root)
    store, queue, source_id = _runtime(config)

    first = QQNTScanner(config, store, queue, source_id=source_id).scan(mode="initial", max_messages=3)
    assert first.scanned_messages == 3
    assert first.has_more is True

    restarted_store = CollectorStateStore(config.paths.state_db)
    restarted_store.initialize()
    restarted_queue = CollectorQueue(restarted_store)
    second = QQNTScanner(config, restarted_store, restarted_queue, source_id=source_id).scan(
        mode="initial",
        max_messages=3,
    )
    third = QQNTScanner(config, restarted_store, restarted_queue, source_id=source_id).scan(
        mode="initial",
        max_messages=3,
    )

    assert second.scanned_messages == 3
    assert third.scanned_messages == 1
    assert third.has_more is False
    assert restarted_queue.counts()["messages"] == {"pending": 7}


def test_large_history_uses_repeated_pages_without_skips(tmp_path):
    data_root = tmp_path / "qqnt"
    database_dir = data_root / "database"
    database_dir.mkdir(parents=True)
    database_path = database_dir / "nt_msg.db"
    _create_large_database(database_path, 503)
    config = _write_config(tmp_path / "collector.toml", tmp_path / "collector-data", data_root)
    store, queue, source_id = _runtime(config)

    result = QQNTScanner(config, store, queue, source_id=source_id).scan(mode="initial")

    assert result.scanned_messages == 503
    assert result.cursors_updated > 10
    assert queue.counts()["messages"] == {"pending": 503}


def test_media_index_stops_at_configured_file_cap(tmp_path):
    media_root = tmp_path / "media"
    media_root.mkdir()
    for index in range(25):
        (media_root / f"image-{index}.jpg").write_bytes(f"image-{index}".encode())

    index = MediaFileIndex([media_root], max_files=10)

    assert index.build() == 10
    assert index.find(file_name="image-0.jpg") == (media_root / "image-0.jpg").resolve()


def test_scanner_cursor_uses_declared_text_order_for_numeric_looking_ids(tmp_path):
    data_root = tmp_path / "qqnt"
    database_dir = data_root / "database"
    database_dir.mkdir(parents=True)
    database_path = database_dir / "nt_msg.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE group_msg_001 (
                msgId TEXT NOT NULL, msgSeq INTEGER NOT NULL, msgTime INTEGER NOT NULL,
                peerUid TEXT NOT NULL, senderUid TEXT NOT NULL, msgContent TEXT NOT NULL,
                chatType TEXT NOT NULL
            )
            """
        )
        for msg_id in ("2", "10"):
            connection.execute(
                "INSERT INTO group_msg_001 VALUES(?,?,?,?,?,?,?)",
                (msg_id, 1, 1000, "group-a", "user-a", "text", "group"),
            )
    config = _write_config(tmp_path / "collector.toml", tmp_path / "collector-data", data_root)
    store, queue, source_id = _runtime(config)

    result = QQNTScanner(config, store, queue, source_id=source_id).scan(mode="initial")

    with store.connect() as connection:
        cursor = json.loads(connection.execute("SELECT cursor_json FROM table_cursors").fetchone()["cursor_json"])
    assert result.scanned_messages == 2
    assert cursor["last_msg_id"] == "2"
