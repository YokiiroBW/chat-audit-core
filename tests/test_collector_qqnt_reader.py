from __future__ import annotations

import sqlite3

import pytest

from collector.qqnt.discovery import candidate_data_roots, discover_database_sets, discover_qqnt_data
from collector.app.main import main
from collector.qqnt.key_provider import SQLiteKeyValidator
from collector.qqnt.capabilities import detect_runtime_capabilities
from collector.qqnt.reader import ReadOnlyDatabaseError, ReadOnlySQLiteDatabase
from collector.qqnt.schema_probe import probe_schema
from collector.qqnt.snapshot import DatabaseSnapshotManager
from collector.qqnt.snapshot import detect_database_format
from collector.qqnt.versions import select_adapter
from collector.sync.cursor import CursorRepository, IncrementalCursor
from collector.sync.state import CollectorStateStore


def _create_message_database(path):
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute(
        """
        CREATE TABLE group_msg_001 (
            msgId TEXT NOT NULL,
            msgSeq INTEGER NOT NULL,
            msgTime INTEGER NOT NULL,
            peerUid TEXT NOT NULL,
            senderUid TEXT NOT NULL,
            msgContent BLOB NOT NULL,
            chatType TEXT NOT NULL
        )
        """
    )
    connection.executemany(
        "INSERT INTO group_msg_001(msgId,msgSeq,msgTime,peerUid,senderUid,msgContent,chatType) VALUES(?,?,?,?,?,?,?)",
        [
            ("1", 1, 1000, "group-a", "user-a", b"first", "group"),
            ("2", 2, 1000, "group-a", "user-b", b"second", "group"),
            ("3", 1, 1010, "group-b", "user-c", b"third", "group"),
        ],
    )
    connection.commit()
    return connection


def _write_collector_config(path, root, data_root):
    root_text = str(root).replace("\\", "\\\\")
    data_root_text = str(data_root).replace("\\", "\\\\")
    path.write_text(
        f"""
[collector]
account_id = "123456789"
device_id = "test-device"
device_name = "Test PC"
data_dir = "{root_text}"

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


def test_qqnt_discovery_is_scoped_and_reports_wal_family(tmp_path):
    root = tmp_path / "Tencent" / "QQ" / "nt_data"
    database_dir = root / "database"
    database_dir.mkdir(parents=True)
    database = database_dir / "nt_msg.db"
    database.write_bytes(b"database")
    (database_dir / "nt_msg.db-wal").write_bytes(b"wal")
    (database_dir / "nt_msg.db-shm").write_bytes(b"shm")
    unrelated = root / "cache" / "random.db"
    unrelated.parent.mkdir()
    unrelated.write_bytes(b"ignored")

    data_sets = discover_database_sets(root)

    assert len(data_sets) == 1
    assert [item.path.name for item in data_sets[0].databases] == ["nt_msg.db"]
    assert data_sets[0].databases[0].wal_path.name == "nt_msg.db-wal"
    assert data_sets[0].databases[0].shm_path.name == "nt_msg.db-shm"
    assert candidate_data_roots(configured_root=root, environment={}) == [root.resolve()]
    assert candidate_data_roots(configured_root=root, environment={"USERPROFILE": str(tmp_path / "other")}) == [
        root.resolve()
    ]
    assert discover_qqnt_data(configured_root=root, environment={})[0].root == root.resolve()


def test_read_only_database_probe_and_composite_cursor_pagination(tmp_path):
    database_path = tmp_path / "nt_msg.db"
    writer = _create_message_database(database_path)
    try:
        before = database_path.stat().st_mtime_ns
        database = ReadOnlySQLiteDatabase(database_path)
        report = probe_schema(database)
        adapter = select_adapter(report)

        assert report.message_candidates[0].table == "group_msg_001"
        assert adapter.name == "qqnt-9.9.x-basic"
        first_page = adapter.read_page(
            database,
            report.message_candidates[0],
            IncrementalCursor(),
            limit=2,
        )
        second_page = adapter.read_page(
            database,
            report.message_candidates[0],
            first_page.next_cursor,
            limit=2,
        )

        assert [row.msg_id for row in first_page.rows] == ["1", "2"]
        assert [row.msg_id for row in second_page.rows] == ["3"]
        assert first_page.next_cursor == IncrementalCursor(1000, 2, "2", "2")
        overlap = adapter.read_page(
            database,
            report.message_candidates[0],
            second_page.next_cursor,
            limit=10,
            overlap_seconds=60,
        )
        assert [row.msg_id for row in overlap.rows] == ["1", "2", "3"]
        assert database_path.stat().st_mtime_ns == before

        with pytest.raises(ReadOnlyDatabaseError, match="only read-only SQL"):
            database.query("INSERT INTO group_msg_001(msgId) VALUES('x')")
        with pytest.raises(sqlite3.DatabaseError):
            with database.read_transaction() as connection:
                connection.execute("DELETE FROM group_msg_001")
    finally:
        writer.close()


def test_plain_sqlite_snapshot_includes_committed_wal_rows(tmp_path):
    database_path = tmp_path / "nt_msg.db"
    writer = _create_message_database(database_path)
    snapshots = DatabaseSnapshotManager(tmp_path / "snapshots")
    try:
        snapshot = snapshots.create(database_path)
        rows = ReadOnlySQLiteDatabase(snapshot.database_path, immutable=True).query(
            "SELECT msgId FROM group_msg_001 ORDER BY msgId"
        )
        assert [row["msgId"] for row in rows] == ["1", "2", "3"]
        assert snapshot.method == "sqlite_backup"
        assert len(snapshot.content_sha256) == 64
    finally:
        writer.close()
        snapshots.cleanup(snapshot)


def test_private_message_table_basic_fields_are_read_without_parsing_content(tmp_path):
    database_path = tmp_path / "c2c_msg.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE c2c_msg_001 (
                msgId INTEGER NOT NULL,
                msgSeq INTEGER NOT NULL,
                msgTime INTEGER NOT NULL,
                peerUid TEXT NOT NULL,
                senderUid TEXT NOT NULL,
                msgContent BLOB NOT NULL,
                chatType TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO c2c_msg_001 VALUES(?,?,?,?,?,?,?)",
            (10, 3, 1020, "peer-a", "sender-a", b"protobuf-bytes", "private"),
        )
    database = ReadOnlySQLiteDatabase(database_path)
    report = probe_schema(database)
    adapter = select_adapter(report)

    page = adapter.read_page(database, report.message_candidates[0], IncrementalCursor(), limit=10)

    assert report.message_candidates[0].chat_kind == "private"
    assert page.rows[0].msg_id == "10"
    assert page.rows[0].conversation_id == "peer-a"
    assert page.rows[0].sender_id == "sender-a"
    assert page.rows[0].content == b"protobuf-bytes"


def test_non_sqlite_snapshot_copies_database_wal_and_shm_together(tmp_path):
    database = tmp_path / "encrypted.db"
    database.write_bytes(b"encrypted-header")
    (tmp_path / "encrypted.db-wal").write_bytes(b"wal")
    (tmp_path / "encrypted.db-shm").write_bytes(b"shm")
    manager = DatabaseSnapshotManager(tmp_path / "snapshots")

    snapshot = manager.create(database)

    assert snapshot.method == "verified_family_copy"
    assert (snapshot.snapshot_dir / "encrypted.db").read_bytes() == b"encrypted-header"
    assert (snapshot.snapshot_dir / "encrypted.db-wal").read_bytes() == b"wal"
    assert (snapshot.snapshot_dir / "encrypted.db-shm").read_bytes() == b"shm"


def test_database_key_validation_and_cursor_repository(tmp_path):
    database_path = tmp_path / "plain.db"
    writer = _create_message_database(database_path)
    writer.close()
    assert SQLiteKeyValidator().validate(database_path, "unused").status == "not_required"

    encrypted = tmp_path / "encrypted.db"
    encrypted.write_bytes(b"not-a-sqlite-header")
    assert SQLiteKeyValidator().validate(encrypted, "configured-key").status == "unsupported"

    qqnt_custom = tmp_path / "nt_msg.db"
    qqnt_custom.write_bytes(b"SQLite header 3\x00" + b"\x00" * 16 + b"QQ_NT DB" + b"\x00" * 32)
    qqnt_result = SQLiteKeyValidator().validate(qqnt_custom, "unused")
    assert detect_database_format(qqnt_custom) == "qqnt_custom_vfs"
    assert qqnt_result.status == "invalid"
    assert qqnt_result.error_code == "DB_KEY_INVALID"

    store = CollectorStateStore(tmp_path / "state.sqlite3")
    store.initialize()
    store.upsert_source(
        source_id="source",
        source_type="qqnt_local_db",
        account_id="123",
        device_id="pc",
    )
    cursors = CursorRepository(store, "source")
    cursor = IncrementalCursor(1000, 2, "message", "row")
    cursors.save_table("group_msg_001", cursor)
    assert cursors.load_table("group_msg_001") == cursor
    assert IncrementalCursor(1783000000000).overlap_start(3600) == 1782996400000


def test_runtime_capabilities_do_not_mistake_stdlib_sqlite_for_sqlcipher():
    capabilities = detect_runtime_capabilities()

    assert capabilities.sqlite_version
    assert capabilities.nt_vfs_available is False
    if capabilities.sqlcipher_available:
        assert capabilities.sqlcipher_provider in {"pysqlcipher3", "sqlcipher3"}
    else:
        assert capabilities.sqlcipher_provider is None


def test_collector_cli_discover_and_probe_plain_sqlite(tmp_path, capsys):
    data_root = tmp_path / "qqnt"
    database_dir = data_root / "database"
    database_dir.mkdir(parents=True)
    database_path = database_dir / "nt_msg.db"
    writer = _create_message_database(database_path)
    writer.close()
    config_path = tmp_path / "collector.toml"
    _write_collector_config(config_path, tmp_path / "collector-data", data_root)

    assert main(["--config", str(config_path), "discover", "--json"]) == 0
    discover_output = capsys.readouterr().out
    assert "nt_msg.db" in discover_output

    assert main(["--config", str(config_path), "probe", str(database_path)]) == 0
    probe_output = capsys.readouterr().out
    assert '"adapter": "qqnt-9.9.x-basic"' in probe_output
    assert '"schema_fingerprint"' in probe_output
