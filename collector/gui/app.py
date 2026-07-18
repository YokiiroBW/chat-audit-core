from __future__ import annotations

import argparse
import ctypes
import queue
import threading
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable

from collector import __version__
from collector.gui.controller import CollectorGuiController, GuiSettings, default_gui_config_path
from collector.gui.tray import TrayManager
from collector.gui.single_instance import AlreadyRunningError, SingleInstance


def _count_status(values: dict[str, int], *statuses: str) -> int:
    return sum(int(values.get(status, 0)) for status in statuses)


def _mask_account(value: str) -> str:
    if len(value) <= 5:
        return value
    return value[:2] + "*" * max(3, len(value) - 4) + value[-2:]


class CollectorTrayApp:
    def __init__(self, config_path: str | Path | None = None, *, start_hidden: bool = False) -> None:
        self.root = tk.Tk()
        self.root.title("Chat Audit QQ Collector")
        self.root.geometry("820x610")
        self.root.minsize(760, 560)
        self.root.protocol("WM_DELETE_WINDOW", self.hide_window)
        self.start_hidden = start_hidden
        self.events: queue.Queue[tuple[str, str]] = queue.Queue()
        self.ui_callbacks: queue.Queue[Callable[[], None]] = queue.Queue()
        self.busy = False
        self._refresh_after_id: str | None = None

        self.controller = CollectorGuiController(config_path, event_callback=self._post_event)
        self.tray = TrayManager(
            show=lambda: self._tk_call(self.show_window),
            start=lambda: self._tk_call(self.start_sync),
            stop=lambda: self._tk_call(self.stop_sync),
            sync_once=lambda: self._tk_call(self.sync_once),
            diagnostics=lambda: self._tk_call(self.create_diagnostics),
            exit_app=lambda: self._tk_call(self.exit_app),
        )

        self._configure_style()
        self._create_variables()
        self._build_ui()
        self._load_settings()
        self._refresh_status()
        self._drain_events()
        self._run_task(self.controller.inspect_databases, self._show_compatibility, quiet=True)

    def _configure_style(self) -> None:
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 18, "bold"))
        style.configure("Subtitle.TLabel", foreground="#64748b", font=("Microsoft YaHei UI", 9))
        style.configure("Metric.TLabel", font=("Microsoft YaHei UI", 20, "bold"))
        style.configure("Status.TLabel", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Accent.TButton", font=("Microsoft YaHei UI", 10, "bold"))

    def _create_variables(self) -> None:
        self.status_var = tk.StringVar(value="未配置")
        self.account_status_var = tk.StringVar(value="—")
        self.server_status_var = tk.StringVar(value="—")
        self.compatibility_var = tk.StringVar(value="正在检测数据库…")
        self.message_pending_var = tk.StringVar(value="0")
        self.media_pending_var = tk.StringVar(value="0")
        self.failure_var = tk.StringVar(value="0")
        self.last_sync_var = tk.StringVar(value="从未同步")

        self.account_var = tk.StringVar()
        self.device_var = tk.StringVar(value="Windows PC")
        self.data_root_var = tk.StringVar()
        self.server_url_var = tk.StringVar(value="http://127.0.0.1:8001")
        self.token_var = tk.StringVar()
        self.interval_var = tk.IntVar(value=10)
        self.verify_tls_var = tk.BooleanVar(value=True)
        self.token_state_var = tk.StringVar(value="未保存")

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=18)
        outer.pack(fill="both", expand=True)

        header = ttk.Frame(outer)
        header.pack(fill="x", pady=(0, 12))
        ttk.Label(header, text="Chat Audit QQ Collector", style="Title.TLabel").pack(side="left")
        ttk.Label(header, textvariable=self.status_var, style="Status.TLabel").pack(side="right", padx=8)
        ttk.Label(header, text=f"v{__version__}", style="Subtitle.TLabel").pack(side="right")

        self.notebook = ttk.Notebook(outer)
        self.notebook.pack(fill="both", expand=True)
        self.dashboard_tab = ttk.Frame(self.notebook, padding=16)
        self.settings_tab = ttk.Frame(self.notebook, padding=16)
        self.logs_tab = ttk.Frame(self.notebook, padding=16)
        self.notebook.add(self.dashboard_tab, text="状态")
        self.notebook.add(self.settings_tab, text="设置")
        self.notebook.add(self.logs_tab, text="运行记录")
        self._build_dashboard()
        self._build_settings()
        self._build_logs()

    def _build_dashboard(self) -> None:
        identity = ttk.LabelFrame(self.dashboard_tab, text="连接状态", padding=12)
        identity.pack(fill="x")
        ttk.Label(identity, text="QQ 账号：").grid(row=0, column=0, sticky="w")
        ttk.Label(identity, textvariable=self.account_status_var).grid(row=0, column=1, sticky="w", padx=(4, 24))
        ttk.Label(identity, text="服务器：").grid(row=0, column=2, sticky="w")
        ttk.Label(identity, textvariable=self.server_status_var).grid(row=0, column=3, sticky="w", padx=4)
        ttk.Label(identity, text="数据库：").grid(row=1, column=0, sticky="nw", pady=(10, 0))
        ttk.Label(identity, textvariable=self.compatibility_var, wraplength=610).grid(
            row=1, column=1, columnspan=3, sticky="w", padx=(4, 0), pady=(10, 0)
        )
        identity.columnconfigure(3, weight=1)

        metrics = ttk.Frame(self.dashboard_tab)
        metrics.pack(fill="x", pady=14)
        for column, (title, variable) in enumerate(
            (
                ("待上传消息", self.message_pending_var),
                ("待上传媒体", self.media_pending_var),
                ("失败/待处理", self.failure_var),
            )
        ):
            card = ttk.LabelFrame(metrics, text=title, padding=14)
            card.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 7, 0))
            ttk.Label(card, textvariable=variable, style="Metric.TLabel").pack()
            metrics.columnconfigure(column, weight=1)

        last = ttk.Frame(self.dashboard_tab)
        last.pack(fill="x", pady=(0, 14))
        ttk.Label(last, text="上次同步：").pack(side="left")
        ttk.Label(last, textvariable=self.last_sync_var).pack(side="left")

        actions = ttk.LabelFrame(self.dashboard_tab, text="操作", padding=12)
        actions.pack(fill="x")
        buttons = (
            ("开始后台同步", self.start_sync, "Accent.TButton"),
            ("暂停", self.stop_sync, None),
            ("立即同步", self.sync_once, None),
            ("首次导入", self.initial_import, None),
            ("模拟连接测试", self.simulate_upload, None),
            ("重试失败项", self.retry_failures, None),
            ("生成诊断包", self.create_diagnostics, None),
        )
        for index, (text, command, style) in enumerate(buttons):
            ttk.Button(actions, text=text, command=command, style=style or "TButton").grid(
                row=index // 4, column=index % 4, sticky="ew", padx=5, pady=5
            )
        for column in range(4):
            actions.columnconfigure(column, weight=1)

    def _build_settings(self) -> None:
        form = ttk.Frame(self.settings_tab)
        form.pack(fill="x")
        labels = ("QQ 账号", "设备名称", "QQNT 数据目录", "服务器地址", "API Token", "同步间隔（分钟）")
        for row, text in enumerate(labels):
            ttk.Label(form, text=text).grid(row=row, column=0, sticky="w", pady=7)
        ttk.Entry(form, textvariable=self.account_var).grid(row=0, column=1, columnspan=2, sticky="ew", pady=7)
        ttk.Entry(form, textvariable=self.device_var).grid(row=1, column=1, columnspan=2, sticky="ew", pady=7)
        ttk.Entry(form, textvariable=self.data_root_var).grid(row=2, column=1, sticky="ew", pady=7)
        ttk.Button(form, text="浏览…", command=self.browse_data_root).grid(row=2, column=2, padx=(8, 0))
        ttk.Entry(form, textvariable=self.server_url_var).grid(row=3, column=1, columnspan=2, sticky="ew", pady=7)
        ttk.Entry(form, textvariable=self.token_var, show="●").grid(row=4, column=1, sticky="ew", pady=7)
        ttk.Label(form, textvariable=self.token_state_var, style="Subtitle.TLabel").grid(row=4, column=2, padx=(8, 0))
        ttk.Spinbox(form, from_=1, to=1440, textvariable=self.interval_var, width=10).grid(
            row=5, column=1, sticky="w", pady=7
        )
        ttk.Checkbutton(form, text="验证 HTTPS 证书", variable=self.verify_tls_var).grid(
            row=6, column=1, sticky="w", pady=7
        )
        form.columnconfigure(1, weight=1)

        controls = ttk.Frame(self.settings_tab)
        controls.pack(fill="x", pady=18)
        ttk.Button(controls, text="自动发现 QQ 目录", command=self.auto_detect).pack(side="left")
        ttk.Button(controls, text="保存配置", command=self.save_settings, style="Accent.TButton").pack(
            side="left", padx=8
        )
        ttk.Button(controls, text="测试服务器连接", command=self.test_connection).pack(side="left")
        ttk.Button(controls, text="重新检测数据库", command=self.inspect_databases).pack(side="left", padx=8)

        ttk.Label(
            self.settings_tab,
            text="Token 使用当前 Windows 用户的 DPAPI 加密保存，不会写入 collector.toml。留空保存不会覆盖已有 Token。",
            style="Subtitle.TLabel",
            wraplength=700,
        ).pack(anchor="w")

    def _build_logs(self) -> None:
        self.log_text = tk.Text(
            self.logs_tab,
            height=20,
            state="disabled",
            wrap="word",
            font=("Consolas", 9),
            background="#0f172a",
            foreground="#e2e8f0",
            insertbackground="white",
        )
        scrollbar = ttk.Scrollbar(self.logs_tab, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    def _load_settings(self) -> None:
        settings = self.controller.load_settings()
        if settings is None:
            self.notebook.select(self.settings_tab)
            self._append_log("首次启动：请填写 QQ 账号、数据目录、服务器地址和 Token。")
            return
        self.account_var.set(settings.account_id)
        self.device_var.set(settings.device_name)
        self.data_root_var.set(settings.data_root)
        self.server_url_var.set(settings.server_url)
        self.interval_var.set(settings.sync_interval_minutes)
        self.verify_tls_var.set(settings.verify_tls)
        self.token_state_var.set("已安全保存" if self.controller.has_api_token() else "未保存")
        if not settings.data_root or not Path(settings.data_root).is_dir():
            self.notebook.select(self.settings_tab)
            self._append_log("现有配置的数据目录不可用，请重新选择 QQNT 数据目录。")

    def _settings_value(self) -> GuiSettings:
        return GuiSettings(
            account_id=self.account_var.get(),
            device_name=self.device_var.get(),
            data_root=self.data_root_var.get(),
            server_url=self.server_url_var.get(),
            api_token=self.token_var.get(),
            sync_interval_minutes=int(self.interval_var.get()),
            verify_tls=bool(self.verify_tls_var.get()),
        )

    def _post_event(self, event: str, detail: str) -> None:
        self.events.put((event, detail))

    def _drain_events(self) -> None:
        while True:
            try:
                callback = self.ui_callbacks.get_nowait()
            except queue.Empty:
                break
            callback()
        while True:
            try:
                event, detail = self.events.get_nowait()
            except queue.Empty:
                break
            self._append_log(f"{event}: {detail}")
            if event.endswith("failed"):
                self.tray.notify(detail, "Collector 运行失败")
        self.root.after(500, self._drain_events)

    def _append_log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{timestamp}] {message}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _tk_call(self, callback: Callable[[], None]) -> None:
        try:
            self.root.after(0, callback)
        except tk.TclError:
            pass

    def _run_task(
        self,
        function: Callable[[], Any],
        success: Callable[[Any], None] | None = None,
        *,
        quiet: bool = False,
    ) -> None:
        if self.busy:
            if not quiet:
                messagebox.showinfo("请稍候", "已有操作正在执行。")
            return
        self.busy = True

        def worker() -> None:
            try:
                result = function()
            except Exception as exc:
                detail = str(exc)
                self.ui_callbacks.put(lambda: self._task_failed(detail, quiet=quiet))
            else:
                self.ui_callbacks.put(lambda: self._task_succeeded(result, success, quiet=quiet))

        threading.Thread(target=worker, name="collector-gui-task", daemon=True).start()

    def _task_failed(self, detail: str, *, quiet: bool) -> None:
        self.busy = False
        self._append_log(f"操作失败：{detail}")
        if not quiet:
            messagebox.showerror("操作失败", detail)
        self._refresh_status()

    def _task_succeeded(self, result: Any, success: Callable[[Any], None] | None, *, quiet: bool) -> None:
        self.busy = False
        if success:
            success(result)
        elif not quiet:
            self._append_log("操作完成")
        self._refresh_status()

    def _refresh_status(self) -> None:
        if self._refresh_after_id is not None:
            try:
                self.root.after_cancel(self._refresh_after_id)
            except tk.TclError:
                pass
            self._refresh_after_id = None
        try:
            snapshot = self.controller.status_snapshot()
            configured = snapshot.get("configured", False)
            running = snapshot.get("running", False)
            self.status_var.set("● 运行中" if running else "○ 已暂停" if configured else "未配置")
            self.account_status_var.set(_mask_account(str(snapshot.get("account_id") or "—")))
            self.server_status_var.set(str(snapshot.get("server_url") or "—"))
            queues = snapshot.get("queues") or {}
            messages = queues.get("messages") or {}
            media = queues.get("media") or {}
            self.message_pending_var.set(str(_count_status(messages, "pending", "retry", "inflight")))
            self.media_pending_var.set(str(_count_status(media, "pending", "retry", "inflight")))
            failures = int(snapshot.get("unresolved_parser_failures") or 0)
            failures += _count_status(messages, "dead_letter") + _count_status(media, "dead_letter")
            self.failure_var.set(str(failures))
            last_run = snapshot.get("last_run")
            if last_run:
                self.last_sync_var.set(f"{last_run.get('mode')} / {last_run.get('status')}")
            else:
                self.last_sync_var.set("从未同步")
        except Exception as exc:
            self.status_var.set("配置错误")
            self._append_log(f"状态读取失败：{exc}")
        self._refresh_after_id = self.root.after(3000, self._refresh_status)

    def _show_compatibility(self, report: Any) -> None:
        self.compatibility_var.set(report.message)
        self._append_log(f"数据库检测：{report.message}")

    def browse_data_root(self) -> None:
        selected = filedialog.askdirectory(title="选择 QQNT 数据目录")
        if selected:
            self.data_root_var.set(selected)

    def auto_detect(self) -> None:
        account_id = self.account_var.get()

        def success(value: str | None) -> None:
            if value:
                self.data_root_var.set(value)
                self._append_log(f"已发现 QQNT 数据目录：{value}")
            else:
                messagebox.showwarning("未发现", "未自动发现 QQNT 数据目录，请手动选择。")

        self._run_task(lambda: self.controller.auto_detect_data_root(account_id), success)

    def save_settings(self) -> None:
        def success(_config: Any) -> None:
            self.token_var.set("")
            self.token_state_var.set("已安全保存" if self.controller.has_api_token() else "未保存")
            self._append_log("配置已保存")
            self.inspect_databases()

        self._run_task(lambda: self.controller.save_settings(self._settings_value()), success)

    def test_connection(self) -> None:
        self._run_task(
            self.controller.test_connection,
            lambda detail: messagebox.showinfo("连接成功", detail),
        )

    def inspect_databases(self) -> None:
        self.compatibility_var.set("正在检测数据库…")
        self._run_task(self.controller.inspect_databases, self._show_compatibility)

    def start_sync(self) -> None:
        try:
            started = self.controller.start()
        except Exception as exc:
            messagebox.showerror("无法启动", str(exc))
            self._append_log(f"无法启动后台同步：{exc}")
            return
        self._append_log("后台同步已启动" if started else "后台同步已经在运行")
        self._refresh_status()

    def stop_sync(self) -> None:
        stopped = self.controller.stop()
        self._append_log("后台同步已停止" if stopped else "后台同步未运行或仍在结束当前操作")
        self._refresh_status()

    def sync_once(self) -> None:
        self._run_task(
            lambda: self.controller.run_once("incremental"),
            lambda result: self._append_log(f"同步完成：{result.get('status')}")
        )

    def initial_import(self) -> None:
        if not messagebox.askyesno("首次导入", "将读取一批历史消息并上传，是否继续？"):
            return
        self._run_task(
            lambda: self.controller.run_once("initial"),
            lambda result: self._append_log(f"首次导入批次完成：{result.get('status')}")
        )

    def simulate_upload(self) -> None:
        self._run_task(
            self.controller.simulate_upload,
            lambda result: messagebox.showinfo("模拟测试完成", f"状态：{result.get('status')}")
        )

    def retry_failures(self) -> None:
        self._run_task(
            self.controller.retry_all_dead_letters,
            lambda count: messagebox.showinfo("重试队列", f"已重新入队 {count} 项。"),
        )

    def create_diagnostics(self) -> None:
        self._run_task(
            self.controller.create_diagnostics,
            lambda path: messagebox.showinfo("诊断包已生成", str(path)),
        )

    def show_window(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def hide_window(self) -> None:
        self.root.withdraw()
        self.tray.notify("Collector 已最小化到系统托盘")

    def exit_app(self) -> None:
        self.controller.stop(timeout=5)
        self.tray.stop()
        self.root.destroy()

    def _start_tray(self) -> None:
        def worker() -> None:
            try:
                self.tray.start()
            except Exception as exc:
                self._post_event("tray.failed", str(exc))

        threading.Thread(target=worker, name="collector-gui-tray", daemon=True).start()

    def run(self) -> None:
        if self.start_hidden:
            self.root.withdraw()
        else:
            self.root.deiconify()
        self.root.after(250, self._start_tray)
        self.root.mainloop()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="chat-audit-qq-collector-gui")
    parser.add_argument("--config", default=str(default_gui_config_path()))
    parser.add_argument("--start-hidden", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        with SingleInstance(args.config):
            app = CollectorTrayApp(args.config, start_hidden=args.start_hidden)
            app.run()
    except AlreadyRunningError as exc:
        ctypes.windll.user32.MessageBoxW(None, str(exc), "Chat Audit QQ Collector", 0x40)
        return 1
    return 0


__all__ = ["CollectorTrayApp", "build_parser", "main"]
