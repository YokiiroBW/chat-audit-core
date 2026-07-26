from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import sys
import threading
try:
    import winreg
except ImportError:
    winreg = None
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from collector.app.config import CollectorConfig, default_collector_root, load_config, transport_security_warnings
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
from collector.qqnt.key_provider import SQLiteKeyValidator
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
    qq_install_dir: str = ""
    api_token: str = ""
    qqnt_database_key: str = ""
    sync_interval_minutes: int = 10
    sync_interval_seconds: int | None = None
    verify_tls: bool = True
    collector_data_dir: str = ""
    autostart: bool = False


def _powershell_single_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


# 允许启动同步的兼容性状态。needs_verification 也放行:QQNT 加密库的可读性只能由扫描器
# 真实打开剥头副本来证明,拦在这里等于永久禁用 QQNT 单来源部署。真实失败会以 ScanIssue
# 的 DB_* 停止码上报。
SYNCABLE_COMPATIBILITY_STATUSES = frozenset({"ready", "needs_verification"})


@dataclass(frozen=True)
class CompatibilityReport:
    """兼容性检测结果。

    ``status`` 取值:``ready``(有已证实可读的库)、``needs_verification``(QQNT 加密库通过了
    格式/runtime/密钥形态检查,但可读性尚未实测)、``needs_key``、``unsupported``、``missing``、
    ``unconfigured``。``readable_count`` 只统计**已证实可读**的库,不含待实测的。
    """

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
        self._config_cache: tuple[Any, Any] | None = None
        self._store_cache: tuple[Any, Any] | None = None
        self._api_token_present: bool | None = None
        self._recover_interrupted_runs()

    def _recover_interrupted_runs(self) -> None:
        if not self.configured:
            return
        try:
            config = load_config(self.config_path)
            config.paths.ensure()
            store = CollectorStateStore(config.paths.state_db)
            store.initialize()
            recovered = store.recover_interrupted_runs()
            if recovered:
                self.event_callback(
                    "scheduler.recovered",
                    f"recovered {recovered} interrupted sync run(s)",
                )
        except Exception:
            return

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
            qq_install_dir=str(config.qq.install_dir or ""),
            sync_interval_minutes=config.collector.sync_interval_minutes,
            sync_interval_seconds=config.collector.sync_interval_seconds,
            verify_tls=config.server.verify_tls,
            collector_data_dir=str(config.paths.root),
            autostart=config.collector.autostart,
        )

    @staticmethod
    def _is_runtime_lock(name: str) -> bool:
        return name.startswith("collector-gui-") and name.endswith(".lock")

    def _target_can_resume(self, source: Path, target: Path) -> bool:
        if not source.is_dir() or not target.is_dir():
            return False
        for current, directories, files in os.walk(target):
            relative = Path(current).relative_to(target)
            source_current = source / relative
            for name in [*directories, *files]:
                if self._is_runtime_lock(name):
                    continue
                target_entry = Path(current) / name
                source_entry = source_current / name
                if not source_entry.exists() or source_entry.is_dir() != target_entry.is_dir():
                    return False
        return True

    def _migrate_data_dir(self, source: Path, target: Path) -> None:
        source = source.expanduser().resolve()
        target = target.expanduser().resolve()
        if source == target:
            target.mkdir(parents=True, exist_ok=True)
            return
        if self.running:
            raise RuntimeError("同步正在运行，请先停止同步后再迁移解析器数据目录")
        if source in target.parents or target in source.parents:
            raise ValueError("新目录不能位于旧目录内部，旧目录也不能位于新目录内部")
        if target.exists() and any(target.iterdir()) and not self._target_can_resume(source, target):
            raise ValueError("新目录必须为空，或只能包含上次迁移已经复制的文件")
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(
                source,
                target,
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("collector-gui-*.lock"),
            )
        else:
            target.mkdir(parents=True, exist_ok=True)
        if source.is_dir() and (source / "collector_state.sqlite3").exists():
            if not (target / "collector_state.sqlite3").exists():
                raise RuntimeError("迁移校验失败：状态数据库未复制到新目录")
        # The state database just moved; anything cached still points at the old
        # location, where only leftovers remain.
        self._invalidate_runtime_cache()
        self.event_callback("storage.migrated", f"{source}|{target}")

    @staticmethod
    def _installer_startup_shortcut() -> Path | None:
        """The Startup-folder shortcut the installer optionally creates.

        It is a second, independent autostart source: with both active the
        collector launches twice at login, and clearing the checkbox only
        removed the registry value, so autostart could not actually be turned
        off. The registry entry is kept as the single source because it carries
        the config path this instance is actually using.
        """
        appdata = os.environ.get("APPDATA")
        if not appdata:
            return None
        return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "Chat Audit QQ Collector.lnk"

    def _remove_installer_startup_shortcut(self) -> None:
        shortcut = self._installer_startup_shortcut()
        if shortcut is None:
            return
        try:
            shortcut.unlink(missing_ok=True)
        except OSError:
            # Not fatal: the registry entry still decides whether we start.
            self.event_callback("autostart.shortcut_not_removed", str(shortcut))

    def _set_autostart(self, enabled: bool) -> None:
        if winreg is None:
            if enabled:
                raise RuntimeError("开机自动启动仅支持 Windows")
            return
        # Taken over either way: enabling makes the registry entry the only
        # source, disabling has to clear both or nothing changes at login.
        self._remove_installer_startup_shortcut()
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        value_name = "ChatAuditQQCollector"
        if enabled:
            if getattr(sys, "frozen", False):
                command = f'"{sys.executable}" --start-hidden --config "{self.config_path}"'
            else:
                command = f'"{sys.executable}" -m collector.gui --start-hidden --config "{self.config_path}"'
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                winreg.SetValueEx(key, value_name, 0, winreg.REG_SZ, command)
        else:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
                    winreg.DeleteValue(key, value_name)
            except FileNotFoundError:
                pass

    def save_settings(self, settings: GuiSettings) -> CollectorConfig:
        account_id = settings.account_id.strip()
        device_name = settings.device_name.strip()
        data_root = str(Path(settings.data_root).expanduser().resolve()) if settings.data_root.strip() else ""
        qq_install_dir = str(Path(settings.qq_install_dir).expanduser().resolve()) if settings.qq_install_dir.strip() else ""
        server_url = settings.server_url.strip().rstrip("/")
        if not account_id:
            raise ValueError("QQ 账号不能为空")
        if not device_name:
            raise ValueError("设备名称不能为空")
        if not data_root or not Path(data_root).is_dir():
            raise ValueError("请选择存在的 QQNT 数据目录")
        if not server_url.startswith(("http://", "https://")):
            raise ValueError("服务器地址必须以 http:// 或 https:// 开头")
        interval_seconds = settings.sync_interval_seconds
        if interval_seconds is None:
            if settings.sync_interval_minutes <= 0:
                raise ValueError("sync interval must be positive")
            interval_seconds = settings.sync_interval_minutes * 60
        elif not 10 <= interval_seconds <= 100:
            raise ValueError("read interval must be between 10 and 100 seconds")

        current_config = load_config(self.config_path) if self.configured else None
        current_data_dir = current_config.paths.root if current_config is not None else self.config_path.parent
        requested_data_dir = settings.collector_data_dir.strip()
        data_dir = (Path(requested_data_dir).expanduser().resolve() if requested_data_dir else current_data_dir).resolve()
        self._migrate_data_dir(current_data_dir, data_dir)
        device_id = socket.gethostname().strip() or "windows-host"
        document = f"""
[collector]
account_id = {_toml_string(account_id)}
device_id = {_toml_string(device_id)}
device_name = {_toml_string(device_name)}
data_dir = {_toml_string(str(data_dir))}
sync_interval_minutes = {max(1, (interval_seconds + 59) // 60)}
sync_interval_seconds = {interval_seconds}
autostart = {str(settings.autostart).lower()}
reconcile_interval_hours = 24
staging_max_gb = 20
initial_import_batch_size = 200
incremental_batch_size = 100

[qq]
data_root = {_toml_string(data_root)}
install_dir = {_toml_string(qq_install_dir)}
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
        self._set_autostart(settings.autostart)
        config.paths.ensure()
        credentials = self._credentials_for_config(config)
        if settings.api_token:
            credentials.set("api_token", settings.api_token)
        if settings.qqnt_database_key:
            credentials.set("qqnt_database_key", settings.qqnt_database_key)
        # The config on disk just changed, so anything derived from it is stale.
        self._invalidate_runtime_cache()
        self.event_callback("settings.saved", "配置已保存")
        # Told at the moment the address is set, where it can still be changed,
        # rather than only in a log file nobody opens.
        for warning in transport_security_warnings(config.server):
            self.event_callback("server.insecure_transport", warning)
        return config

    def generate_unlock_script(self, data_root: str, qq_install_dir: str, output_path: str | Path) -> Path:
        data_path = Path(data_root).expanduser().resolve()
        install_path = Path(qq_install_dir).expanduser().resolve()
        if not data_path.is_dir():
            raise ValueError("\u0051\u0051 \u6570\u636e\u76ee\u5f55\u4e0d\u5b58\u5728\uff0c\u8bf7\u5148\u9009\u62e9\u6b63\u786e\u7684\u76ee\u5f55")
        if not install_path.is_dir():
            raise ValueError("\u0051\u0051 \u5b89\u88c5\u76ee\u5f55\u4e0d\u5b58\u5728\uff0c\u8bf7\u5148\u9009\u62e9\u6b63\u786e\u7684\u76ee\u5f55")
        requested_output = Path(output_path).expanduser().resolve()
        if requested_output.suffix.lower() == ".bat":
            launcher = requested_output
            output = requested_output.with_suffix(".ps1")
        else:
            output = requested_output if requested_output.suffix.lower() == ".ps1" else requested_output.with_suffix(".ps1")
            launcher = output.with_suffix(".bat")
        output.parent.mkdir(parents=True, exist_ok=True)
        data_literal = _powershell_single_quote(str(data_path))
        install_literal = _powershell_single_quote(str(install_path))
        script = f"""# Chat Audit QQ Collector - QQNT database unlock helper
# Generated by the collector. This file only runs when the user executes it.
# It downloads the QQBackup/qq-win-db-key Windows NTQQ extractor and invokes it.
# Close QQ completely and back up important data before running this helper.

$ErrorActionPreference = "Stop"
$qqDataDir = {data_literal}
$qqInstallDir = {install_literal}
$extractorUrl = "https://raw.githubusercontent.com/QQBackup/qq-win-db-key/master/scripts/windows/ntqq/windows_ntqq_get_key.ps1"

Write-Host "=== QQNT database key extraction ===" -ForegroundColor Cyan
Write-Host "QQ data directory: $qqDataDir"
Write-Host "QQ install directory: $qqInstallDir"
if (-not (Test-Path -LiteralPath $qqDataDir -PathType Container)) {{
    throw "QQ data directory does not exist: $qqDataDir"
}}
if (-not (Test-Path -LiteralPath $qqInstallDir -PathType Container)) {{
    throw "QQ install directory does not exist: $qqInstallDir"
}}

$qqProcess = Get-Process -Name QQ,QQNT -ErrorAction SilentlyContinue
if ($qqProcess) {{
    Write-Host "QQ is still running. Close QQ and run this helper again." -ForegroundColor Yellow
    exit 2
}}

$versionsDir = Join-Path $qqInstallDir "versions"
$wrapperCandidates = @(Get-ChildItem -LiteralPath $versionsDir -Filter "wrapper.node" -File -Recurse -ErrorAction SilentlyContinue |
    Where-Object {{ $_.FullName -match "\\\\versions\\\\[^\\\\]+\\\\resources\\\\app\\\\wrapper\\.node$" }} |
    Sort-Object LastWriteTime -Descending)
if ($wrapperCandidates.Count -eq 0) {{
    throw "wrapper.node was not found below versions\\<version>\\resources\\app"
}}
$wrapperNode = $wrapperCandidates[0].FullName
Write-Host "Using wrapper.node: $wrapperNode" -ForegroundColor Green

$tempScript = Join-Path $env:TEMP ("chat-audit-qqnt-key-" + [guid]::NewGuid().ToString("N") + ".ps1")
try {{
    Write-Host "Downloading the extractor to a temporary file..." -ForegroundColor Yellow
    Invoke-WebRequest -UseBasicParsing -Uri $extractorUrl -OutFile $tempScript
    Write-Host "Starting the extractor. Follow its prompts." -ForegroundColor Cyan
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $tempScript -WrapperNodePath $wrapperNode
    if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) {{ exit $LASTEXITCODE }}
}} finally {{
    Remove-Item -LiteralPath $tempScript -Force -ErrorAction SilentlyContinue
}}

Write-Host "Copy the 16-byte ASCII key printed by the extractor into the collector settings." -ForegroundColor Green
Write-Host "This helper does not write collector credentials, upload data, or modify the QQ database." -ForegroundColor Gray
"""
        output.write_text(script, encoding="utf-8")
        launcher_text = f"""@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0{output.name}"
set "exit_code=%ERRORLEVEL%"
echo.
if not "%exit_code%"=="0" echo Script exited with code %exit_code%.
pause
exit /b %exit_code%
"""
        launcher.write_text(launcher_text, encoding="utf-8")
        self.event_callback("unlock_script.generated", f"{output}|{launcher}")
        return launcher

    def _config_signature(self) -> int | None:
        return self.config_path.stat().st_mtime_ns if self.config_path.exists() else None

    def _cached_config(self) -> Any:
        """The parsed config, reused until the file on disk changes.

        The tray polls status every three seconds on the Tk main thread, and
        each poll reparsed the TOML twice -- once directly and once inside
        has_api_token().
        """
        signature = self._config_signature()
        if self._config_cache is not None and self._config_cache[0] == signature:
            return self._config_cache[1]
        config = load_config(self.config_path)
        self._config_cache = (signature, config)
        return config

    def _cached_store(self) -> tuple[Any, Any]:
        """Config plus an initialised state store, reused between polls.

        Kept separate from :meth:`_cached_config` on purpose: opening the state
        database is the expensive half, and callers that only need settings --
        has_api_token() among them -- must not be made to touch it.
        """
        signature = self._config_signature()
        config = self._cached_config()
        if self._store_cache is not None and self._store_cache[0] == signature:
            return config, self._store_cache[1]
        config.paths.ensure()
        store = CollectorStateStore(config.paths.state_db)
        store.initialize()
        self._store_cache = (signature, store)
        return config, store

    def _invalidate_runtime_cache(self) -> None:
        self._config_cache = None
        self._store_cache = None
        self._api_token_present = None

    def has_api_token(self) -> bool:
        if not self.configured:
            return False
        if self._api_token_present is None:
            # DPAPI decryption is not free and nothing about it changes between
            # polls; saving settings clears this.
            self._api_token_present = bool(self._credentials_for_config(self._cached_config()).get("api_token"))
        return self._api_token_present

    def has_database_key(self) -> bool:
        if not self.configured:
            return False
        config = load_config(self.config_path)
        return bool(self._credentials_for_config(config).get("qqnt_database_key"))

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
        database_key = self._credentials_for_config(config).get("qqnt_database_key")
        readable = 0
        custom = 0
        pending = 0
        for database, database_format in zip(databases, formats):
            if database_format == "sqlite":
                readable += 1
            elif database_format == "qqnt_custom_vfs":
                custom += 1
                # 只有真正打开成功才算 readable。密钥长度正确不等于密钥正确，而证明它需要
                # 整库剥头复制(真实库数百 MB),不能放在每次兼容性检测里做,所以这里只统计
                # "待实测",由首次同步时扫描器的真实打开来确认。
                result = SQLiteKeyValidator().validate(database.path, database_key)
                if result.usable:
                    readable += 1
                elif result.status == "unverified":
                    pending += 1
        unsupported = len(formats) - readable - pending
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
        if pending:
            return CompatibilityReport(
                "needs_verification",
                f"发现 {len(databases)} 个消息库，其中 {pending} 个是 QQNT 加密库，"
                "密钥格式已通过；能否读取将在首次同步时实测确认",
                len(databases),
                readable,
                unsupported,
                custom,
            )
        if custom:
            return CompatibilityReport(
                "needs_key",
                "检测到 QQNT 加密数据库，请填写 16 字节数据库密钥",
                len(databases),
                readable,
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
        config, store = self._cached_store()
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
            if compatibility.status not in SYNCABLE_COMPATIBILITY_STATUSES:
                raise RuntimeError(compatibility.message)
        return asyncio.run(_run_once(self.config_path, mode, progress_callback=self.event_callback))

    def simulate_upload(self) -> dict[str, Any]:
        config, store, queue, _credentials = _load_runtime(self.config_path)
        message_id = f"gui-sim-{int(time.time())}"
        queue.enqueue_message(
            source_id=_upsert_local_source(store, config),
            dedupe_key=f"simulation:{message_id}",
            payload=_simulated_message(config, message_id),
        )
        return asyncio.run(
            _run_once(self.config_path, "incremental", scan=False, progress_callback=self.event_callback)
        )

    async def _background_main(self) -> None:
        config, store, queue, credentials = _load_runtime(self.config_path)
        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        if self._stop_requested.is_set():
            self._stop_event.set()
        async with CollectorApiClient(config, lambda: credentials.get("api_token")) as api:
            uploader = _build_uploader(config, store, queue, api, progress_callback=self.event_callback)
            scheduler = CollectorScheduler(
                config,
                store,
                uploader,
                _build_scanner(config, store, queue, credentials, progress_callback=self.event_callback),
            )
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
        if compatibility.status not in SYNCABLE_COMPATIBILITY_STATUSES:
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
