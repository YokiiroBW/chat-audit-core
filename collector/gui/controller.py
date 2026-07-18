from __future__ import annotations

import asyncio
import json
import os
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from collector.app.config import CollectorConfig, default_collector_root, load_config
from collector.app.credential_store import CredentialStore
from collector.app.diagnostics import create_diagnostic_bundle
from collector.app.main import (
    _build_scanner,
    _build_uploader,
    _load_runtime,
    _run_once,
    _simulated_message,
    _upsert_local_source,
)
from collector.qqnt.discovery import candidate_data_roots, discover_qqnt_data
from collector.qqnt.snapshot import detect_database_format
from collector.sync.scheduler import CollectorScheduler
from collector.sync.state import CollectorStateStore
from collector.sync.uploader import CollectorApiClient


def default_gui_config_path() -> Path:
    return default_collector_root() / "collector.toml"


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


@dataclass(frozen=True)
class GuiSettings:
    account_id: str
    device_name: str
    data_root: str
    server_url: str
    api_token: str = ""
    sync_interval_minutes: int = 10
    verify_tls: bool = True


@dataclass(frozen=True)
class CompatibilityReport:
    status: str
    message: str
    database_count: int
    readable_count: int
    unsupported_count: int
    qqnt_custom_count: int


class CollectorGuiController:
    def __init__(
        self,
        config_path: str | Path | None = None,
        *,
        credential_store_factory: Callable[[Path], CredentialStore] = CredentialStore,
        event_callback: Callable[[str, str], None] | None = None,
    ) -> None:
        self.config_path = Path(config_path or default_gui_config_path()).expanduser().resolve()
        self.credential_store_factory = credential_store_factory
        self.event_callback = event_callback or (lambda _event, _detail: None)
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._stop_requested = threading.Event()
        self._lock = threading.Lock()

    @property
    def configured(self) -> bool:
        return self.config_path.is_file()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _credentials_for_config(self, config: CollectorConfig) -> CredentialStore:
        return self.credential_store_factory(config.paths.credential_store)

    def load_settings(self) -> GuiSettings | None:
        if not self.configured:
            return None
        config = load_config(self.config_path)
        return GuiSettings(
            account_id=config.collector.account_id,
            device_name=config.collector.device_name,
            data_root=str(config.qq.data_root or ""),
            server_url=config.server.base_url,
            sync_interval_minutes=config.collector.sync_interval_minutes,
            verify_tls=config.server.verify_tls,
        )

    def save_settings(self, settings: GuiSettings) -> CollectorConfig:
        account_id = settings.account_id.strip()
        device_name = settings.device_name.strip()
        data_root = str(Path(settings.data_root).expanduser().resolve()) if settings.data_root.strip() else ""
        server_url = settings.server_url.strip().rstrip("/")
        if not account_id:
            raise ValueError("QQ 账号不能为空")
        if not device_name:
            raise ValueError("设备名称不能为空")
        if not data_root or not Path(data_root).is_dir():
            raise ValueError("请选择存在的 QQNT 数据目录")
        if not server_url.startswith(("http://", "https://")):
            raise ValueError("服务器地址必须以 http:// 或 https:// 开头")
        if settings.sync_interval_minutes <= 0:
            raise ValueError("同步间隔必须大于 0")

        data_dir = self.config_path.parent
        device_id = socket.gethostname().strip() or "windows-host"
        document = f"""
[collector]
account_id = {_toml_string(account_id)}
device_id = {_toml_string(device_id)}
device_name = {_toml_string(device_name)}
data_dir = {_toml_string(str(data_dir))}
sync_interval_minutes = {settings.sync_interval_minutes}
reconcile_interval_hours = 24
staging_max_gb = 20
initial_import_batch_size = 200
incremental_batch_size = 100

[qq]
data_root = {_toml_string(data_root)}
read_mode = "snapshot_copy"
enable_vss_fallback = false

[server]
base_url = {_toml_string(server_url)}
request_timeout_seconds = 60
verify_tls = {str(settings.verify_tls).lower()}

[media]
copy_before_upload = true
validate_video_with_ffprobe = true
upload_thumbnails = true
rescan_not_downloaded_days = 30
""".strip()
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.config_path.with_suffix(self.config_path.suffix + ".tmp")
        temporary.write_text(document + "\n", encoding="utf-8")
        os.replace(temporary, self.config_path)
        config = load_config(self.config_path)
        config.paths.ensure()
        if settings.api_token:
            self._credentials_for_config(config).set("api_token", settings.api_token)
        self.event_callback("settings.saved", "配置已保存")
        return config

    def has_api_token(self) -> bool:
        if not self.configured:
            return False
        config = load_config(self.config_path)
        return bool(self._credentials_for_config(config).get("api_token"))

    def auto_detect_data_root(self, account_id: str = "") -> str | None:
        for root in candidate_data_roots(account_id=account_id.strip() or None):
            for candidate in (root / "nt_qq", root):
                if (candidate / "nt_db").is_dir():
                    return str(candidate.resolve())
            if discover_qqnt_data(configured_root=root, account_id=account_id.strip() or None):
                return str(root.resolve())
        return None

    def inspect_databases(self) -> CompatibilityReport:
        if not self.configured:
            return CompatibilityReport("unconfigured", "请先完成配置", 0, 0, 0, 0)
        config = load_config(self.config_path)
        databases = [
            database
            for data_set in discover_qqnt_data(
                configured_root=config.qq.data_root,
                account_id=config.collector.account_id,
            )
            for database in data_set.databases
            if "msg" in database.path.name.lower()
        ]
        formats = [detect_database_format(database.path) for database in databases]
        readable = sum(value == "sqlite" for value in formats)
        custom = sum(value == "qqnt_custom_vfs" for value in formats)
        unsupported = len(formats) - readable
        if not databases:
            return CompatibilityReport("missing", "未发现 QQNT 消息数据库", 0, 0, 0, 0)
        if readable:
            return CompatibilityReport(
                "ready",
                f"发现 {len(databases)} 个消息库，其中 {readable} 个可读取",
                len(databases),
                readable,
                unsupported,
                custom,
            )
        if custom:
            return CompatibilityReport(
                "unsupported",
                "检测到 QQNT 自定义数据库，当前版本缺少兼容 NT VFS",
                len(databases),
                0,
                unsupported,
                custom,
            )
        return CompatibilityReport(
            "unsupported",
            "消息数据库格式暂不兼容",
            len(databases),
            0,
            unsupported,
            custom,
        )

    def status_snapshot(self) -> dict[str, Any]:
        if not self.configured:
            return {
                "configured": False,
                "running": self.running,
                "queues": {"messages": {}, "media": {}},
                "unresolved_parser_failures": 0,
                "last_run": None,
            }
        config = load_config(self.config_path)
        config.paths.ensure()
        store = CollectorStateStore(config.paths.state_db)
        store.initialize()
        snapshot = store.status_snapshot()
        snapshot.update(
            {
                "configured": True,
                "running": self.running,
                "account_id": config.collector.account_id,
                "server_url": config.server.base_url,
                "has_api_token": self.has_api_token(),
            }
        )
        return snapshot

    async def _test_connection_async(self) -> str:
        config, _store, _queue, credentials = _load_runtime(self.config_path)
        async with CollectorApiClient(config, lambda: credentials.get("api_token")) as api:
            source = await api.register_source()
        source_id = source.get("id")
        return f"连接成功，来源 ID：{source_id}" if source_id else "连接成功"

    def test_connection(self) -> str:
        return asyncio.run(self._test_connection_async())

    def run_once(self, mode: str = "incremental") -> dict[str, Any]:
        if mode != "media_rescan":
            compatibility = self.inspect_databases()
            if compatibility.status != "ready":
                raise RuntimeError(compatibility.message)
        return asyncio.run(_run_once(self.config_path, mode))

    def simulate_upload(self) -> dict[str, Any]:
        config, store, queue, _credentials = _load_runtime(self.config_path)
        message_id = f"gui-sim-{int(time.time())}"
        queue.enqueue_message(
            source_id=_upsert_local_source(store, config),
            dedupe_key=f"simulation:{message_id}",
            payload=_simulated_message(config, message_id),
        )
        return asyncio.run(_run_once(self.config_path, "incremental", scan=False))

    async def _background_main(self) -> None:
        config, store, queue, credentials = _load_runtime(self.config_path)
        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        if self._stop_requested.is_set():
            self._stop_event.set()
        async with CollectorApiClient(config, lambda: credentials.get("api_token")) as api:
            uploader = _build_uploader(config, store, queue, api)
            scheduler = CollectorScheduler(config, store, uploader, _build_scanner(config, store, queue, credentials))
            await scheduler.run_forever(self._stop_event)

    def _background_thread(self) -> None:
        self.event_callback("scheduler.started", "后台同步已启动")
        try:
            asyncio.run(self._background_main())
        except Exception as exc:
            self.event_callback("scheduler.failed", str(exc))
        finally:
            self._loop = None
            self._stop_event = None
            self.event_callback("scheduler.stopped", "后台同步已停止")

    def start(self) -> bool:
        if not self.configured:
            raise RuntimeError("请先完成配置")
        compatibility = self.inspect_databases()
        if compatibility.status != "ready":
            raise RuntimeError(compatibility.message)
        if not self.has_api_token():
            raise RuntimeError("请先保存 API Token")
        with self._lock:
            if self.running:
                return False
            self._stop_requested.clear()
            self._thread = threading.Thread(target=self._background_thread, name="collector-gui-sync", daemon=True)
            self._thread.start()
        return True

    def stop(self, *, timeout: float = 5.0) -> bool:
        with self._lock:
            thread = self._thread
            loop = self._loop
            stop_event = self._stop_event
        if thread is None or not thread.is_alive():
            return False
        self._stop_requested.set()
        if loop is not None and stop_event is not None:
            loop.call_soon_threadsafe(stop_event.set)
        thread.join(timeout=timeout)
        return not thread.is_alive()

    def retry_all_dead_letters(self) -> int:
        _config, store, queue, _credentials = _load_runtime(self.config_path)
        count = 0
        with store.connect() as connection:
            items = [
                (kind, str(row["id"]))
                for kind, table in (("message", "pending_messages"), ("media", "pending_media"))
                for row in connection.execute(f"SELECT id FROM {table} WHERE status='dead_letter'").fetchall()
            ]
        for kind, queue_id in items:
            count += int(queue.requeue_dead_letter(kind, queue_id))
        return count

    def create_diagnostics(self) -> Path:
        config, store, _queue, _credentials = _load_runtime(self.config_path)
        return create_diagnostic_bundle(config, store)


__all__ = [
    "CollectorGuiController",
    "CompatibilityReport",
    "GuiSettings",
    "default_gui_config_path",
]
