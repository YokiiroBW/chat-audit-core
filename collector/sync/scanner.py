from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from collector.app.config import CollectorConfig
from collector.app.logging import redact
from collector.media.indexer import MediaFileIndex
from collector.media.resolver import MediaResolver
from collector.media.staging import MediaStagingStore
from collector.parsers.message import MessageParserContext, enqueue_parsed_message, parse_source_message
from collector.qqnt.discovery import discover_qqnt_data
from collector.qqnt.key_provider import SQLiteKeyValidator
from collector.qqnt.reader import QQNTReader, ReadOnlySQLiteDatabase
from collector.qqnt.profiles import load_profile_index
from collector.qqnt.snapshot import DatabaseSnapshot, DatabaseSnapshotManager, detect_database_format
from collector.qqnt.sqlcipher import materialize_clear_database
from collector.qqnt.versions import select_adapter
from collector.sync.cursor import CursorRepository, IncrementalCursor
from collector.sync.queue import CollectorQueue
from collector.sync.state import CollectorStateStore


MODE_OVERLAP_SECONDS = {
    "initial": 0,
    "incremental": 3600,
    "reconcile": 86400,
}


@dataclass(frozen=True)
class ScanIssue:
    database: str
    error_code: str
    detail: str


@dataclass
class ScanResult:
    mode: str
    discovered_databases: int = 0
    scanned_databases: int = 0
    scanned_tables: int = 0
    scanned_messages: int = 0
    enqueued_messages: int = 0
    parser_failures: int = 0
    media_indexed_files: int = 0
    cursors_updated: int = 0
    has_more: bool = False
    issues: list[ScanIssue] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["partial"] = bool(self.issues)
        return value


def _database_cursor_name(database_path: Path, table_name: str) -> str:
    normalized = str(database_path.resolve()).casefold()
    database_id = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return f"{database_path.name}:{database_id}:{table_name}"


def _database_paths(config: CollectorConfig, explicit: Iterable[str | Path] | None) -> tuple[Path, ...]:
    if explicit:
        paths = [Path(path).expanduser().resolve() for path in explicit]
    else:
        paths = [
            database.path
            for data_set in discover_qqnt_data(
                configured_root=config.qq.data_root,
                account_id=config.collector.account_id,
            )
            for database in data_set.databases
            if database.role in {"group_message", "private_message", "message"}
        ]
    return tuple(dict.fromkeys(paths))


def _media_roots(config: CollectorConfig, database_paths: tuple[Path, ...]) -> tuple[Path, ...]:
    roots: list[Path] = []
    if config.qq.data_root is not None and config.qq.data_root.expanduser().is_dir():
        roots.append(config.qq.data_root.expanduser().resolve())
    roots.extend(path.parent for path in database_paths)
    return tuple(dict.fromkeys(roots))


class QQNTScanner:
    def __init__(
        self,
        config: CollectorConfig,
        store: CollectorStateStore,
        queue: CollectorQueue,
        *,
        source_id: str,
        credential_getter: Callable[[str], str | None] | None = None,
        progress_callback: Callable[[str, str], None] | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.queue = queue
        self.source_id = source_id
        self.credential_getter = credential_getter or (lambda _name: None)
        self.progress_callback = progress_callback or (lambda _event, _detail: None)

    def _emit_progress(self, event: str, detail: str) -> None:
        # Progress details are database paths, and a QQ data directory is named
        # after the account. These reach the tray log, which users screenshot
        # and paste into issue reports, so they get the same redaction the log
        # file already applies.
        self.progress_callback(event, str(redact(detail)))

    def _resolver(self, database_paths: tuple[Path, ...], result: ScanResult) -> MediaResolver:
        roots = _media_roots(self.config, database_paths)
        if not roots:
            return MediaResolver()
        index = MediaFileIndex(roots)
        result.media_indexed_files = index.build()
        # Same roots bound the explicit paths read out of the database, so a row
        # cannot point the collector at a file outside the QQ data tree.
        return MediaResolver(index, allowed_roots=roots)

    def _database_key(self, database_path: Path) -> tuple[str | None, str | None, str | None]:
        validator = SQLiteKeyValidator()
        validation = validator.validate(database_path, None)
        key = None
        if validation.error_code == "DB_KEY_INVALID":
            key = self.credential_getter("qqnt_database_key")
            validation = validator.validate(database_path, key)
        # may_attempt, not usable: a custom-VFS database is only proven readable by the real
        # open a few lines below, whose failure is recorded as a ScanIssue.
        if not validation.may_attempt:
            return None, validation.error_code or "DB_OPEN_FAILED", validation.detail or "database is not usable"
        return key if validation.requires_key else None, None, None

    def scan(
        self,
        *,
        mode: str = "incremental",
        database_paths: Iterable[str | Path] | None = None,
        max_messages: int | None = None,
    ) -> ScanResult:
        if mode not in MODE_OVERLAP_SECONDS:
            raise ValueError(f"unsupported scan mode: {mode}")
        if max_messages is not None and max_messages <= 0:
            raise ValueError("max_messages must be positive")

        paths = _database_paths(self.config, database_paths)
        result = ScanResult(mode=mode, discovered_databases=len(paths))
        self._emit_progress("scan.started", f"{mode}|{len(paths)}")
        resolver = self._resolver(paths, result)
        staging = MediaStagingStore(
            self.config.paths.staging_dir,
            max_bytes=self.config.collector.staging_max_gb * 1024 * 1024 * 1024,
        )
        cursors = CursorRepository(self.store, self.source_id)
        snapshot_manager = DatabaseSnapshotManager(self.config.paths.root / "snapshots")
        profile_index = load_profile_index(
            paths,
            key=self.credential_getter("qqnt_database_key"),
            snapshots=snapshot_manager,
        )
        page_size = min(
            1000,
            self.config.collector.initial_import_batch_size
            if mode == "initial"
            else self.config.collector.incremental_batch_size,
        )

        for source_path in paths:
            self._emit_progress("scan.database", str(source_path))
            if max_messages is not None and result.scanned_messages >= max_messages:
                result.has_more = True
                break
            key, error_code, detail = self._database_key(source_path)
            if error_code:
                result.issues.append(ScanIssue(str(source_path), error_code, detail or error_code))
                continue

            snapshot: DatabaseSnapshot | None = None
            target_path = source_path
            cipher = False
            try:
                is_qqnt_custom = detect_database_format(source_path) == "qqnt_custom_vfs"
                if not is_qqnt_custom and self.config.qq.read_mode != "snapshot_copy":
                    # Reading the live file means the scan sees whatever QQ has
                    # already flushed, and a busy WAL keeps moving underneath a
                    # paged read. Not refused -- copying a multi-hundred-megabyte
                    # database on every cycle is its own problem -- but the
                    # operator should know which semantics they are getting.
                    wal_path = source_path.with_name(source_path.name + "-wal")
                    if wal_path.exists() and wal_path.stat().st_size > 0:
                        self._emit_progress(
                            "scan.live_read",
                            f"{source_path}|QQ 正在写入该库，本次扫描只能看到已落盘的数据；"
                            "需要一致性快照请设置 [qq].read_mode = \"snapshot_copy\"",
                        )
                if self.config.qq.read_mode == "snapshot_copy" or is_qqnt_custom:
                    snapshot = snapshot_manager.create(source_path)
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
                    key=key,
                    cipher=cipher,
                )
                reader = QQNTReader(database)
                report = reader.probe()
                adapter = select_adapter(report)
                context = MessageParserContext(
                    account_id=self.config.collector.account_id,
                    schema_version=report.fingerprint,
                    sender_nicknames=profile_index.sender_nicknames,
                    room_names=profile_index.room_names,
                    sender_avatars=profile_index.sender_avatars,
                    room_avatars=profile_index.room_avatars,
                )
                result.scanned_databases += 1

                for candidate in adapter.candidates(report):
                    if max_messages is not None and result.scanned_messages >= max_messages:
                        result.has_more = True
                        break
                    result.scanned_tables += 1
                    cursor_name = _database_cursor_name(source_path, candidate.table)
                    saved_cursor = cursors.load_table(cursor_name)
                    page_cursor = IncrementalCursor() if mode == "reconcile" else saved_cursor
                    first_page = True
                    while True:
                        remaining = (
                            page_size
                            if max_messages is None
                            else min(page_size, max_messages - result.scanned_messages)
                        )
                        if remaining <= 0:
                            result.has_more = True
                            break
                        page = adapter.read_page(
                            database,
                            candidate,
                            page_cursor,
                            limit=remaining,
                            overlap_seconds=MODE_OVERLAP_SECONDS[mode] if first_page else 0,
                        )
                        first_page = False
                        if not page.rows:
                            break
                        # One transaction per page: enqueueing used to open a
                        # connection and fsync per message, which made a large
                        # initial import disk-bound rather than parse-bound.
                        with self.store.transaction():
                            for row in page.rows:
                                parsed = parse_source_message(row, context, resolver=resolver)
                                enqueue_parsed_message(
                                    parsed,
                                    source_id=self.source_id,
                                    queue=self.queue,
                                    store=self.store,
                                    staging=staging,
                                    force_refresh=mode == "reconcile",
                                )
                                result.scanned_messages += 1
                                result.enqueued_messages += 1
                                if result.scanned_messages % 100 == 0:
                                    self._emit_progress(
                                        "scan.progress",
                                        f"{result.scanned_messages}|{result.enqueued_messages}|{result.parser_failures}",
                                    )
                                result.parser_failures += len(parsed.failures)
                        page_cursor = page.next_cursor
                        if adapter.cursor_after(database, candidate, page_cursor, saved_cursor):
                            saved_cursor = page_cursor
                            cursors.save_table(cursor_name, saved_cursor)
                            result.cursors_updated += 1
                        if len(page.rows) < remaining:
                            break
            except Exception as exc:
                error_code = getattr(exc, "error_code", "COLLECTOR_SCAN_FAILED")
                result.issues.append(ScanIssue(str(source_path), str(error_code), str(exc)))
            finally:
                if snapshot is not None:
                    snapshot_manager.cleanup(snapshot)
        self._emit_progress(
            "scan.completed",
            f"{result.scanned_messages}|{result.enqueued_messages}|{result.parser_failures}|{len(result.issues)}",
        )
        return result


__all__ = ["MODE_OVERLAP_SECONDS", "QQNTScanner", "ScanIssue", "ScanResult"]
