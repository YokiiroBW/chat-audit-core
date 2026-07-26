from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Callable

from collector.app.config import CollectorConfig, CollectorConfigError, load_config
from collector.app.credential_store import CredentialStore, CredentialStoreError
from collector.app.diagnostics import create_diagnostic_bundle
from collector.app.logging import configure_logging
from collector.qqnt.discovery import discover_qqnt_data
from collector.qqnt.key_provider import SQLiteKeyValidator
from collector.qqnt.reader import QQNTReader, ReadOnlyDatabaseError, ReadOnlySQLiteDatabase
from collector.qqnt.snapshot import DatabaseSnapshotManager, SnapshotError, detect_database_format
from collector.qqnt.sqlcipher import materialize_clear_database
from collector.qqnt.versions import select_adapter
from collector.qqnt.versions.base import SchemaAdapterError
from collector.sync.queue import CollectorQueue
from collector.sync.scheduler import CollectorScheduler
from collector.sync.scanner import QQNTScanner
from collector.sync.state import CollectorStateError, CollectorStateStore
from collector.sync.uploader import CollectorApiClient, QueueUploader


DEFAULT_CONFIG_NAME = "collector.toml"
EXAMPLE_CONFIG = Path(__file__).resolve().parents[1] / "collector.example.toml"


def _local_source_id(config: CollectorConfig) -> str:
    identity = f"qqnt_local_db:{config.collector.account_id}:{config.collector.device_id}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _upsert_local_source(store: CollectorStateStore, config: CollectorConfig, *, status: str = "active") -> str:
    source_id = _local_source_id(config)
    store.upsert_source(
        source_id=source_id,
        source_type="qqnt_local_db",
        account_id=config.collector.account_id,
        device_id=config.collector.device_id,
        status=status,
        metadata={"device_name": config.collector.device_name},
    )
    return source_id


def _load_runtime(config_path: str | Path) -> tuple[CollectorConfig, CollectorStateStore, CollectorQueue, CredentialStore]:
    config = load_config(config_path)
    config.paths.ensure()
    configure_logging(config.paths.log_dir)
    store = CollectorStateStore(config.paths.state_db)
    store.initialize()
    queue = CollectorQueue(store)
    credentials = CredentialStore(config.paths.credential_store)
    return config, store, queue, credentials


def _build_uploader(
    config: CollectorConfig,
    store: CollectorStateStore,
    queue: CollectorQueue,
    api: CollectorApiClient,
    progress_callback: Callable[[str, str], None] | None = None,
) -> QueueUploader:
    return QueueUploader(config, store, queue, api, progress_callback=progress_callback)


def _build_scanner(
    config: CollectorConfig,
    store: CollectorStateStore,
    queue: CollectorQueue,
    credentials: CredentialStore,
    progress_callback: Callable[[str, str], None] | None = None,
) -> QQNTScanner:
    return QQNTScanner(
        config,
        store,
        queue,
        source_id=_upsert_local_source(store, config),
        credential_getter=credentials.get,
        progress_callback=progress_callback,
    )


def _write_initial_config(path: Path, *, force: bool) -> None:
    if path.exists() and not force:
        raise CollectorConfigError(f"config already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(EXAMPLE_CONFIG, path)


def _simulated_message(config: CollectorConfig, message_id: str) -> dict[str, Any]:
    timestamp = int(time.time())
    source_key = f"simulated:{message_id}"
    return {
        "message": {
            "robot_id": config.collector.account_id,
            "platform": "qq",
            "room_id": "collector-simulation",
            "message_type": "group",
            "sender_id": config.collector.account_id,
            "nickname": "Collector Simulation",
            "raw_message": f"Collector simulated message {message_id}",
            "local_message": f"Collector simulated message {message_id}",
            "timestamp": timestamp,
            "message_id": message_id,
        },
        "source_record": {
            "source_table": "collector_simulation",
            "source_key": source_key,
            "schema_version": "simulation-v1",
            "raw_columns": {"kind": "simulation"},
            "metadata": {"simulated": True},
        },
        "media": [
            {
                "ordinal": 0,
                "media_type": "video",
                "source_state": "not_downloaded",
                "archive_state": "metadata_only",
                "file_name": "not-downloaded.mp4",
                "failure_code": "MEDIA_LOCAL_NOT_DOWNLOADED",
                "metadata": {"simulated": True},
            }
        ],
    }


async def _run_once(
    config_path: str | Path,
    mode: str,
    *,
    scan: bool = True,
    progress_callback: Callable[[str, str], None] | None = None,
) -> dict[str, Any]:
    config, store, queue, credentials = _load_runtime(config_path)
    async with CollectorApiClient(config, lambda: credentials.get("api_token")) as api:
        uploader = _build_uploader(config, store, queue, api, progress_callback=progress_callback)
        scanner = (
            _build_scanner(config, store, queue, credentials, progress_callback=progress_callback)
            if scan
            else None
        )
        scheduler = CollectorScheduler(config, store, uploader, scanner)
        result = await scheduler.run_once(mode)
    return {
        "run_id": result.run_id,
        "mode": result.mode,
        "status": result.status,
        "error_code": result.error_code,
        "stats": result.stats,
    }


async def _run_forever(config_path: str | Path) -> None:
    config, store, queue, credentials = _load_runtime(config_path)
    async with CollectorApiClient(config, lambda: credentials.get("api_token")) as api:
        uploader = _build_uploader(config, store, queue, api)
        scheduler = CollectorScheduler(config, store, uploader, _build_scanner(config, store, queue, credentials))
        await scheduler.run_forever()


def _credential_command(args: argparse.Namespace) -> int:
    _, _, _, credentials = _load_runtime(args.config)
    if args.credential_action == "set":
        value = getpass.getpass(f"Credential value for {args.name}: ")
        if not value:
            raise CredentialStoreError("empty credentials are not allowed")
        credentials.set(args.name, value)
        print(f"Stored credential: {args.name}")
    elif args.credential_action == "delete":
        print("Deleted" if credentials.delete(args.name) else "Credential not found")
    elif args.credential_action == "list":
        for name in credentials.list_names():
            print(name)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="chat-audit-qq-collector")
    parser.add_argument("--config", default=DEFAULT_CONFIG_NAME, help="Path to collector TOML config")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Write an example collector config")
    init_parser.add_argument("--force", action="store_true")

    credential_parser = subparsers.add_parser("credential", help="Manage DPAPI-protected credentials")
    credential_subparsers = credential_parser.add_subparsers(dest="credential_action", required=True)
    set_parser = credential_subparsers.add_parser("set")
    set_parser.add_argument("name", choices=["api_token", "qqnt_database_key", "client_certificate"])
    delete_parser = credential_subparsers.add_parser("delete")
    delete_parser.add_argument("name", choices=["api_token", "qqnt_database_key", "client_certificate"])
    credential_subparsers.add_parser("list")

    status_parser = subparsers.add_parser("status", help="Show local queue and run status")
    status_parser.add_argument("--json", action="store_true", dest="as_json")

    simulate_parser = subparsers.add_parser("simulate", help="Queue a simulated QQNT message")
    simulate_parser.add_argument("--message-id", default=None)
    simulate_parser.add_argument("--upload", action="store_true")

    run_once_parser = subparsers.add_parser("run-once", help="Run one upload cycle")
    run_once_parser.add_argument(
        "--mode",
        choices=["initial", "incremental", "reconcile", "media_rescan"],
        default="incremental",
    )

    subparsers.add_parser("run", help="Run the Collector scheduler")

    retry_parser = subparsers.add_parser("retry", help="Requeue a dead-letter item")
    retry_parser.add_argument("kind", choices=["message", "media"])
    retry_parser.add_argument("queue_id")

    discover_parser = subparsers.add_parser("discover", help="Discover QQNT database files without opening them")
    discover_parser.add_argument("--json", action="store_true", dest="as_json")

    probe_parser = subparsers.add_parser("probe", help="Open one database read-only and report its schema fingerprint")
    probe_parser.add_argument("database")
    probe_parser.add_argument("--snapshot", action="store_true")

    scan_parser = subparsers.add_parser("scan", help="Parse QQNT messages into the persistent upload queue")
    scan_parser.add_argument("database", nargs="*", help="Optional database paths; otherwise use discovery")
    scan_parser.add_argument("--mode", choices=["initial", "incremental", "reconcile"], default="incremental")
    scan_parser.add_argument("--max-messages", type=int, default=None)
    scan_parser.add_argument("--json", action="store_true", dest="as_json")

    diagnose_parser = subparsers.add_parser("diagnose", help="Create a redacted diagnostic ZIP")
    diagnose_parser.add_argument("--output", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "init":
            path = Path(args.config).expanduser().resolve()
            _write_initial_config(path, force=args.force)
            print(f"Wrote collector config: {path}")
            return 0
        if args.command == "credential":
            return _credential_command(args)

        config, store, queue, credentials = _load_runtime(args.config)
        if args.command == "status":
            snapshot = store.status_snapshot()
            snapshot["paths"] = {
                "state_db": str(config.paths.state_db),
                "staging_dir": str(config.paths.staging_dir),
            }
            if args.as_json:
                print(json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True))
            else:
                print(f"State DB: {config.paths.state_db}")
                print(f"Messages: {snapshot['queues']['messages']}")
                print(f"Media: {snapshot['queues']['media']}")
                print(f"Parser failures: {snapshot['unresolved_parser_failures']}")
                print(f"Last run: {snapshot['last_run'] or 'none'}")
            return 0
        if args.command == "diagnose":
            bundle = create_diagnostic_bundle(config, store, output_path=args.output)
            print(f"Created diagnostic bundle: {bundle}")
            return 0
        if args.command == "simulate":
            local_source_id = _upsert_local_source(store, config)
            message_id = args.message_id or f"collector-sim-{int(time.time())}"
            queue_id = queue.enqueue_message(
                source_id=local_source_id,
                dedupe_key=f"simulation:{message_id}",
                payload=_simulated_message(config, message_id),
            )
            print(f"Queued simulated message: {queue_id}")
            if args.upload:
                print(
                    json.dumps(
                        asyncio.run(_run_once(args.config, "incremental", scan=False)),
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            return 0
        if args.command == "run-once":
            print(json.dumps(asyncio.run(_run_once(args.config, args.mode)), ensure_ascii=False, indent=2))
            return 0
        if args.command == "run":
            try:
                asyncio.run(_run_forever(args.config))
            except KeyboardInterrupt:
                return 0
            return 0
        if args.command == "retry":
            if not queue.requeue_dead_letter(args.kind, args.queue_id):
                print("Dead-letter item not found", file=sys.stderr)
                return 1
            print(f"Requeued {args.kind}: {args.queue_id}")
            return 0
        if args.command == "discover":
            data_sets = discover_qqnt_data(
                configured_root=config.qq.data_root,
                account_id=config.collector.account_id,
            )
            payload = [
                {
                    "root": str(data_set.root),
                    "databases": [
                        {
                            "path": str(database.path),
                            "role": database.role,
                            "size": database.size,
                            "format": detect_database_format(database.path),
                            "has_wal": database.wal_path is not None,
                            "has_shm": database.shm_path is not None,
                        }
                        for database in data_set.databases
                    ],
                }
                for data_set in data_sets
            ]
            if args.as_json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                if not payload:
                    print("No QQNT databases discovered")
                for data_set in payload:
                    print(f"Root: {data_set['root']}")
                    for database in data_set["databases"]:
                        print(
                            f"  {database['role']}: {database['path']} "
                            f"({database['size']} bytes, format={database['format']}, wal={database['has_wal']})"
                        )
            return 0
        if args.command == "scan":
            local_source_id = _upsert_local_source(store, config)
            result = QQNTScanner(
                config,
                store,
                queue,
                source_id=local_source_id,
                credential_getter=credentials.get,
            ).scan(
                mode=args.mode,
                database_paths=args.database or None,
                max_messages=args.max_messages,
            )
            payload = result.to_dict()
            if result.scanned_databases == 0 and result.issues:
                source_status = (
                    "needs_key"
                    if any(issue.error_code == "DB_KEY_INVALID" for issue in result.issues)
                    else "error"
                )
                _upsert_local_source(store, config, status=source_status)
            if args.as_json:
                print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
            else:
                print(
                    f"Scanned {result.scanned_messages} messages from "
                    f"{result.scanned_tables} tables in {result.scanned_databases} databases"
                )
                print(
                    f"Queued: {result.enqueued_messages}; parser failures: {result.parser_failures}; "
                    f"media indexed: {result.media_indexed_files}"
                )
                for issue in result.issues:
                    print(f"warning: {issue.database}: {issue.error_code}: {issue.detail}", file=sys.stderr)
            if result.scanned_databases == 0 and result.discovered_databases == 0:
                print("error: no QQNT message databases discovered", file=sys.stderr)
                return 2
            return 1 if result.issues else 0
        if args.command == "probe":
            database_path = Path(args.database).expanduser().resolve()
            validator = SQLiteKeyValidator()
            key = None
            key_result = validator.validate(database_path, None)
            if key_result.status == "invalid" and key_result.error_code == "DB_KEY_INVALID":
                key = credentials.get("qqnt_database_key")
                key_result = validator.validate(database_path, key)
            if not key_result.may_attempt:
                source_status = "needs_key" if key_result.error_code == "DB_KEY_INVALID" else "error"
                _upsert_local_source(store, config, status=source_status)
                print(f"error: {key_result.error_code}: {key_result.detail}", file=sys.stderr)
                return 2
            _upsert_local_source(store, config, status="active")
            snapshot = None
            manager = None
            target_path = database_path
            cipher = False
            try:
                is_qqnt_custom = detect_database_format(database_path) == "qqnt_custom_vfs"
                if args.snapshot or config.qq.read_mode == "snapshot_copy" or is_qqnt_custom:
                    manager = DatabaseSnapshotManager(config.paths.root / "snapshots")
                    snapshot = manager.create(database_path)
                    target_path = snapshot.database_path
                if is_qqnt_custom:
                    target_path = materialize_clear_database(
                        target_path,
                        snapshot.snapshot_dir / f"{target_path.stem}.clear.db",
                    )
                    cipher = True
                database = ReadOnlySQLiteDatabase(
                    target_path,
                    immutable=snapshot is not None,
                    key=key if key_result.requires_key else None,
                    cipher=cipher,
                )
                report = QQNTReader(database).probe()
                adapter = select_adapter(report)
                print(
                    json.dumps(
                        {
                            "database": database_path.name,
                            "schema_fingerprint": report.fingerprint,
                            "sqlite_version": report.sqlite_version,
                            "user_version": report.user_version,
                            "adapter": adapter.name,
                            "message_tables": [
                                {
                                    "table": candidate.table,
                                    "chat_kind": candidate.chat_kind,
                                    "mapping": candidate.mapping,
                                }
                                for candidate in adapter.candidates(report)
                            ],
                        },
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                )
            finally:
                if snapshot is not None and manager is not None:
                    manager.cleanup(snapshot)
            return 0
    except (
        CollectorConfigError,
        CollectorStateError,
        CredentialStoreError,
        ReadOnlyDatabaseError,
        SchemaAdapterError,
        SnapshotError,
        OSError,
        ValueError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 1
