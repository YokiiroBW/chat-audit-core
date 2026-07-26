from __future__ import annotations

import argparse
import ctypes
import queue
import sys
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


def _ui_text(value: str) -> str:
    return value.encode("ascii").decode("unicode_escape")


def _mask_account(value: str) -> str:
    if len(value) <= 5:
        return value
    return value[:2] + "*" * max(3, len(value) - 4) + value[-2:]


class CollectorTrayApp:
    def __init__(self, config_path: str | Path | None = None, *, start_hidden: bool = False) -> None:
        self.root = tk.Tk()
        self.root.title("Chat Audit QQ Collector")
        self.root.geometry("980x700")
        self.root.minsize(900, 640)
        self.root.protocol("WM_DELETE_WINDOW", self.hide_window)
        self._apply_window_rounding()
        self.start_hidden = start_hidden
        self.events: queue.Queue[tuple[str, str]] = queue.Queue()
        self.ui_callbacks: queue.Queue[Callable[[], None]] = queue.Queue()
        self.busy = False
        self.activity_state = ""
        self.visual_status = "paused"
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

    def _apply_window_rounding(self) -> None:
        if not hasattr(ctypes, "windll"):
            return
        try:
            self.root.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            preference = ctypes.c_int(2)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(preference), ctypes.sizeof(preference))
        except (AttributeError, OSError):
            pass

    def _configure_style(self) -> None:
        self.root.configure(bg="#edf3fb")
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("TNotebook", background="#edf3fb", borderwidth=0)
        style.configure("TNotebook.Tab", padding=(18, 9), font=("Microsoft YaHei UI", 10))
        style.configure("Title.TLabel", background="#edf3fb", foreground="#17233d", font=("Microsoft YaHei UI", 20, "bold"))
        style.configure("Subtitle.TLabel", background="#edf3fb", foreground="#6d7b91", font=("Microsoft YaHei UI", 9))
        style.configure("Metric.TLabel", background="#ffffff", foreground="#17233d", font=("Microsoft YaHei UI", 23, "bold"))
        style.configure("Status.TLabel", background="#edf3fb", foreground="#5b3b91", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Accent.TButton", font=("Microsoft YaHei UI", 10, "bold"))

    def _create_variables(self) -> None:
        self.status_var = tk.StringVar(value="未配置")
        self.page_title_var = tk.StringVar(value=_ui_text(r"\u540c\u6b65\u603b\u89c8"))
        self.page_subtitle_var = tk.StringVar(value=_ui_text(r"\u5b9e\u65f6\u638c\u63e1\u89e3\u6790\u3001\u4e0a\u4f20\u4e0e\u5f02\u5e38\u72b6\u6001"))
        self.account_status_var = tk.StringVar(value="—")
        self.server_status_var = tk.StringVar(value="—")
        self.compatibility_var = tk.StringVar(value="正在检测数据库…")
        self.message_pending_var = tk.StringVar(value="0")
        self.media_pending_var = tk.StringVar(value="0")
        self.failure_var = tk.StringVar(value="0")
        self.analyzed_var = tk.StringVar(value="0")
        self.uploaded_var = tk.StringVar(value="0")
        self.progress_detail_var = tk.StringVar(value=_ui_text(r"\u7b49\u5f85\u9996\u6b21\u540c\u6b65"))
        self.last_sync_var = tk.StringVar(value="从未同步")

        self.account_var = tk.StringVar()
        self.device_var = tk.StringVar(value="Windows PC")
        self.data_root_var = tk.StringVar()
        self.qq_install_dir_var = tk.StringVar()
        self.collector_data_dir_var = tk.StringVar()
        self.server_url_var = tk.StringVar(value="http://127.0.0.1:8001")
        self.token_var = tk.StringVar()
        self.database_key_var = tk.StringVar()
        self.interval_var = tk.IntVar(value=60)
        self.verify_tls_var = tk.BooleanVar(value=True)
        self.token_state_var = tk.StringVar(value="未保存")
        self.database_key_state_var = tk.StringVar(value="未保存")
        self.autostart_var = tk.BooleanVar(value=False)

    def _build_ui(self) -> None:
        outer = tk.Frame(self.root, bg="#edf3fb", padx=18, pady=18)
        outer.pack(fill="both", expand=True)
        shell = tk.Frame(outer, bg="#f8fbff", highlightthickness=0)
        shell.pack(fill="both", expand=True)

        sidebar = tk.Frame(shell, bg="#f0f4fa", width=190, padx=14, pady=18)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        tk.Label(sidebar, text="Chat Audit", bg="#f0f4fa", fg="#17233d", font=("Microsoft YaHei UI", 16, "bold")).pack(anchor="w", padx=8)
        tk.Label(sidebar, text="QQ Collector", bg="#f0f4fa", fg="#8b5cf6", font=("Microsoft YaHei UI", 9, "bold")).pack(anchor="w", padx=8, pady=(0, 28))
        self.nav_buttons = {}
        for key, label in (("dashboard", _ui_text(r"\u72b6\u6001\u603b\u89c8")), ("settings", _ui_text(r"\u8fde\u63a5\u8bbe\u7f6e")), ("logs", _ui_text(r"\u8fd0\u884c\u8bb0\u5f55"))):
            button = tk.Button(sidebar, text=label, command=lambda page=key: self._show_page(page), relief="flat", bd=0, anchor="w", padx=14, pady=10, bg="#f0f4fa", fg="#65738a", activebackground="#e9ddff", activeforeground="#5b3b91", font=("Microsoft YaHei UI", 10))
            button.pack(fill="x", pady=3)
            self.nav_buttons[key] = button
        tk.Frame(sidebar, bg="#dce6f2", height=1).pack(fill="x", padx=8, pady=20)
        self.status_panel = tk.Frame(sidebar, bg="#fff4d6", padx=10, pady=9)
        self.status_panel.pack(fill="x", padx=2)
        status_line = tk.Frame(self.status_panel, bg="#fff4d6")
        status_line.pack(fill="x")
        self.status_dot = tk.Canvas(status_line, width=12, height=12, bg="#fff4d6", highlightthickness=0)
        self.status_dot.pack(side="left", padx=(0, 7))
        self.status_label = tk.Label(status_line, textvariable=self.status_var, bg="#fff4d6", fg="#a66a00", font=("Microsoft YaHei UI", 9, "bold"))
        self.status_label.pack(side="left")
        tk.Label(self.status_panel, text=f"v{__version__}", bg="#fff4d6", fg="#a66a00", font=("Microsoft YaHei UI", 8)).pack(anchor="w", pady=(5, 0))
        self._set_visual_status("paused")

        content = tk.Frame(shell, bg="#f8fbff", padx=18, pady=18)
        content.pack(side="left", fill="both", expand=True)
        header = tk.Frame(content, bg="#f8fbff")
        header.pack(fill="x", pady=(0, 14))
        tk.Label(header, textvariable=self.page_title_var, bg="#f8fbff", fg="#17233d", font=("Microsoft YaHei UI", 20, "bold")).pack(side="left")
        tk.Label(header, textvariable=self.page_subtitle_var, bg="#f8fbff", fg="#7a879a", font=("Microsoft YaHei UI", 9)).pack(side="left", padx=14, pady=(7, 0))

        self.page_container = tk.Frame(content, bg="#f8fbff")
        self.page_container.pack(fill="both", expand=True)
        self.dashboard_tab = tk.Frame(self.page_container, bg="#edf3fb")
        self.settings_tab = tk.Frame(self.page_container, bg="#f8fbff")
        self.logs_tab = tk.Frame(self.page_container, bg="#f8fbff")
        self._build_dashboard()
        self._build_settings()
        self._build_logs()
        self._show_page("dashboard")

    def _set_visual_status(self, status: str) -> None:
        palette = {
            "uploading": ("#dcfce7", "#16a34a", _ui_text(r"\u6b63\u5728\u4e0a\u4f20")),
            "paused": ("#fff4d6", "#a66a00", _ui_text(r"\u5df2\u6682\u505c")),
            "analyzing": ("#eee5ff", "#7c3aed", _ui_text(r"\u6b63\u5728\u89e3\u6790")),
            "error": ("#fee2e2", "#dc2626", _ui_text(r"\u540c\u6b65\u9519\u8bef")),
        }
        background, foreground, _label = palette.get(status, palette["paused"])
        self.visual_status = status
        if hasattr(self, "status_panel"):
            self.status_panel.configure(bg=background)
            self.status_label.configure(bg=background, fg=foreground)
            self.status_dot.configure(bg=background)
            self.status_dot.delete("all")
            self.status_dot.create_oval(2, 2, 10, 10, fill=foreground, outline=foreground)
            for child in self.status_panel.winfo_children():
                if isinstance(child, tk.Label) and child is not self.status_label:
                    child.configure(bg=background, fg=foreground)
                if isinstance(child, tk.Frame):
                    child.configure(bg=background)
                    for nested in child.winfo_children():
                        if nested is not self.status_dot and isinstance(nested, tk.Widget):
                            try:
                                nested.configure(bg=background)
                            except tk.TclError:
                                pass
        self.tray.set_status(status)

    def _show_page(self, page: str) -> None:
        pages = {"dashboard": self.dashboard_tab, "settings": self.settings_tab, "logs": self.logs_tab}
        titles = {"dashboard": (_ui_text(r"\u540c\u6b65\u603b\u89c8"), _ui_text(r"\u5b9e\u65f6\u638c\u63e1\u89e3\u6790\u3001\u4e0a\u4f20\u4e0e\u5f02\u5e38\u72b6\u6001")), "settings": (_ui_text(r"\u8fde\u63a5\u8bbe\u7f6e"), _ui_text(r"\u914d\u7f6e QQNT\u3001\u670d\u52a1\u5668\u4e0e\u8bfb\u53d6\u9891\u7387")), "logs": (_ui_text(r"\u8fd0\u884c\u8bb0\u5f55"), _ui_text(r"\u67e5\u770b\u6700\u8fd1\u7684\u89e3\u6790\u548c\u4e0a\u4f20\u6d3b\u52a8"))}
        for frame in pages.values():
            frame.pack_forget()
        pages[page].pack(fill="both", expand=True)
        title, subtitle = titles[page]
        self.page_title_var.set(title)
        self.page_subtitle_var.set(subtitle)
        for key, button in self.nav_buttons.items():
            selected = key == page
            button.configure(bg="#e9ddff" if selected else "#f0f4fa", fg="#5b3b91" if selected else "#65738a")

    def _card(self, parent: tk.Misc, title: str, variable: tk.StringVar, accent: str, subtitle: str) -> tk.Frame:
        card = tk.Frame(parent, bg="#ffffff", highlightthickness=1, highlightbackground="#dce6f2")
        tk.Frame(card, bg=accent, width=5).pack(side="left", fill="y")
        body = tk.Frame(card, bg="#ffffff", padx=16, pady=13)
        body.pack(side="left", fill="both", expand=True)
        tk.Label(body, text=title, bg="#ffffff", fg="#6d7b91", font=("Microsoft YaHei UI", 10)).pack(anchor="w")
        ttk.Label(body, textvariable=variable, style="Metric.TLabel").pack(anchor="w", pady=(4, 1))
        tk.Label(body, text=subtitle, bg="#ffffff", fg="#9aa7b8", font=("Microsoft YaHei UI", 8)).pack(anchor="w")
        return card

    def _build_dashboard(self) -> None:
        body = tk.Frame(self.dashboard_tab, bg="#edf3fb")
        body.pack(fill="both", expand=True)

        welcome = tk.Frame(body, bg="#ffffff", highlightthickness=1, highlightbackground="#dce6f2")
        welcome.pack(fill="x", pady=(0, 14))
        left = tk.Frame(welcome, bg="#ffffff", padx=20, pady=16)
        left.pack(side="left", fill="x", expand=True)
        tk.Label(left, text=_ui_text(r"\u540c\u6b65\u603b\u89c8"), bg="#ffffff", fg="#17233d", font=("Microsoft YaHei UI", 16, "bold")).pack(anchor="w")
        tk.Label(left, text=_ui_text(r"\u5b9e\u65f6\u67e5\u770b QQ \u6d88\u606f\u89e3\u6790\u3001\u4e0a\u4f20\u4e0e\u5f02\u5e38\u72b6\u6001"), bg="#ffffff", fg="#718096", font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(4, 0))
        tk.Label(welcome, textvariable=self.compatibility_var, bg="#f4f0ff", fg="#6941a5", padx=14, pady=8, font=("Microsoft YaHei UI", 9)).pack(side="right", padx=16, pady=17)

        metrics = tk.Frame(body, bg="#edf3fb")
        metrics.pack(fill="x", pady=(0, 14))
        cards = (
            (_ui_text(r"\u5df2\u5206\u6790\u6d88\u606f"), self.analyzed_var, "#8b5cf6", _ui_text(r"\u5df2\u8fdb\u5165\u89e3\u6790\u961f\u5217")),
            (_ui_text(r"\u5df2\u4e0a\u4f20\u6d88\u606f"), self.uploaded_var, "#22c55e", _ui_text(r"\u5df2\u540c\u6b65\u5230\u670d\u52a1\u5668")),
            (_ui_text(r"\u5f85\u5904\u7406\u5a92\u4f53"), self.media_pending_var, "#f59e0b", _ui_text(r"\u7b49\u5f85\u4e0a\u4f20\u6216\u91cd\u8bd5")),
            (_ui_text(r"\u5f02\u5e38\u9879\u76ee"), self.failure_var, "#ef4444", _ui_text(r"\u9700\u8981\u5173\u6ce8\u7684\u9879\u76ee")),
        )
        for column, (title, variable, accent, subtitle) in enumerate(cards):
            card = self._card(metrics, title, variable, accent, subtitle)
            card.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 8, 0))
            metrics.columnconfigure(column, weight=1)

        lower = tk.Frame(body, bg="#edf3fb")
        lower.pack(fill="both", expand=True)
        progress_card = tk.Frame(lower, bg="#ffffff", highlightthickness=1, highlightbackground="#dce6f2")
        progress_card.pack(side="left", fill="both", expand=True, padx=(0, 8))
        tk.Label(progress_card, text=_ui_text(r"\u540c\u6b65\u8fdb\u5ea6"), bg="#ffffff", fg="#17233d", font=("Microsoft YaHei UI", 13, "bold")).pack(anchor="w", padx=20, pady=(17, 0))
        tk.Label(progress_card, textvariable=self.progress_detail_var, bg="#ffffff", fg="#718096", font=("Microsoft YaHei UI", 9)).pack(anchor="w", padx=20, pady=(4, 0))
        self.progress_canvas = tk.Canvas(progress_card, width=220, height=190, bg="#ffffff", highlightthickness=0)
        self.progress_canvas.pack(side="left", padx=(24, 12), pady=8)
        progress_info = tk.Frame(progress_card, bg="#ffffff")
        progress_info.pack(side="left", fill="x", expand=True, padx=(0, 20))
        self._legend_row(progress_info, "#8b5cf6", _ui_text(r"\u5df2\u5206\u6790"), self.analyzed_var)
        self._legend_row(progress_info, "#22c55e", _ui_text(r"\u5df2\u4e0a\u4f20"), self.uploaded_var)
        self._legend_row(progress_info, "#e6edf5", _ui_text(r"\u5f85\u4e0a\u4f20"), self.message_pending_var)

        status_card = tk.Frame(lower, bg="#ffffff", highlightthickness=1, highlightbackground="#dce6f2")
        status_card.pack(side="left", fill="both", expand=True)
        tk.Label(status_card, text=_ui_text(r"\u8fde\u63a5\u4e0e\u8fd0\u884c\u72b6\u6001"), bg="#ffffff", fg="#17233d", font=("Microsoft YaHei UI", 13, "bold")).pack(anchor="w", padx=20, pady=(17, 12))
        self._status_row(status_card, _ui_text(r"QQ \u8d26\u53f7"), self.account_status_var)
        self._status_row(status_card, _ui_text(r"\u670d\u52a1\u5668"), self.server_status_var)
        self._status_row(status_card, _ui_text(r"\u4e0a\u6b21\u540c\u6b65"), self.last_sync_var)
        actions = tk.Frame(status_card, bg="#ffffff")
        actions.pack(fill="x", padx=20, pady=(18, 16))
        action_buttons = (
            (_ui_text(r"\u5f00\u59cb\u89e3\u6790"), self.initial_import, "#8b5cf6"),
            (_ui_text(r"\u4fee\u590d\u5386\u53f2\u8d44\u6599"), self.repair_history, "#0f766e"),
            (_ui_text(r"\u540e\u53f0\u540c\u6b65"), self.start_sync, "#5b3b91"),
            (_ui_text(r"\u6682\u505c\u540c\u6b65"), self.stop_sync, "#f0a202"),
            (_ui_text(r"\u7acb\u5373\u540c\u6b65"), self.sync_once, "#3b82f6"),
        )
        for index, (text, command, color) in enumerate(action_buttons):
            button = tk.Button(actions, text=text, command=command, relief="flat", bd=0, padx=8, pady=8, bg=color, fg="#ffffff", activebackground=color, activeforeground="#ffffff", font=("Microsoft YaHei UI", 9, "bold"))
            button.grid(row=index // 2, column=index % 2, sticky="ew", padx=(0 if index % 2 == 0 else 6, 6), pady=(0 if index < 2 else 6, 0))
        actions.columnconfigure(0, weight=1)
        actions.columnconfigure(1, weight=1)

    def _legend_row(self, parent: tk.Misc, color: str, title: str, variable: tk.StringVar) -> None:
        row = tk.Frame(parent, bg="#ffffff")
        row.pack(fill="x", pady=7)
        tk.Canvas(row, width=12, height=12, bg="#ffffff", highlightthickness=0).pack(side="left", padx=(0, 8))
        dot = row.winfo_children()[0]
        dot.create_oval(1, 1, 11, 11, fill=color, outline=color)
        tk.Label(row, text=title, bg="#ffffff", fg="#718096", font=("Microsoft YaHei UI", 9)).pack(side="left")
        tk.Label(row, textvariable=variable, bg="#ffffff", fg="#17233d", font=("Microsoft YaHei UI", 11, "bold")).pack(side="right")

    def _status_row(self, parent: tk.Misc, title: str, variable: tk.StringVar) -> None:
        row = tk.Frame(parent, bg="#ffffff")
        row.pack(fill="x", padx=20, pady=6)
        tk.Label(row, text=title, bg="#ffffff", fg="#8a97a9", font=("Microsoft YaHei UI", 9)).pack(side="left")
        tk.Label(row, textvariable=variable, bg="#ffffff", fg="#25324b", font=("Microsoft YaHei UI", 9, "bold"), wraplength=230, justify="right").pack(side="right")

    def _build_settings(self) -> None:
        card = tk.Frame(self.settings_tab, bg="#ffffff", highlightthickness=1, highlightbackground="#dce6f2")
        card.pack(fill="x")
        form = tk.Frame(card, bg="#ffffff", padx=22, pady=18)
        form.pack(fill="x")
        labels = (
            _ui_text(r"\u0051\u0051 \u8d26\u53f7"),
            _ui_text(r"\u8bbe\u5907\u540d\u79f0"),
            _ui_text(r"\u0051\u0051\u004e\u0054 \u6570\u636e\u76ee\u5f55"),
            _ui_text(r"\u0051\u0051 \u5b89\u88c5\u76ee\u5f55"),
            _ui_text(r"\u89e3\u6790\u5668\u6570\u636e\u76ee\u5f55"),
            _ui_text(r"\u670d\u52a1\u5668\u5730\u5740"),
            "API Token",
            _ui_text(r"\u0051\u0051\u004e\u0054 \u6570\u636e\u5e93\u5bc6\u94a5"),
            _ui_text(r"\u8bfb\u53d6\u95f4\u9694\uff08\u79d2\uff09"),
        )
        for row, label in enumerate(labels):
            tk.Label(form, text=label, bg="#ffffff", fg="#526177", font=("Microsoft YaHei UI", 9)).grid(row=row, column=0, sticky="w", pady=7, padx=(0, 18))
        entry_style = {"relief": "flat", "bd": 0, "highlightthickness": 1, "highlightbackground": "#d9e3ef", "highlightcolor": "#8b5cf6", "bg": "#fbfdff", "fg": "#1e2b43", "insertbackground": "#5b3b91", "font": ("Microsoft YaHei UI", 9)}
        tk.Entry(form, textvariable=self.account_var, **entry_style).grid(row=0, column=1, columnspan=2, sticky="ew", pady=5)
        tk.Entry(form, textvariable=self.device_var, **entry_style).grid(row=1, column=1, columnspan=2, sticky="ew", pady=5)
        tk.Entry(form, textvariable=self.data_root_var, **entry_style).grid(row=2, column=1, sticky="ew", pady=5)
        tk.Button(form, text=_ui_text(r"\u6d4f\u89c8\u2026"), command=self.browse_data_root, relief="flat", bd=0, bg="#eef2f8", fg="#526177", padx=12).grid(row=2, column=2, padx=(8, 0))
        tk.Entry(form, textvariable=self.qq_install_dir_var, **entry_style).grid(row=3, column=1, sticky="ew", pady=5)
        tk.Button(form, text=_ui_text(r"\u6d4f\u89c8\u2026"), command=self.browse_qq_install_dir, relief="flat", bd=0, bg="#eef2f8", fg="#526177", padx=12).grid(row=3, column=2, padx=(8, 0))
        tk.Entry(form, textvariable=self.collector_data_dir_var, **entry_style).grid(row=4, column=1, sticky="ew", pady=5)
        tk.Button(form, text=_ui_text(r"\u6d4f\u89c8\u2026"), command=self.browse_collector_data_dir, relief="flat", bd=0, bg="#eef2f8", fg="#526177", padx=12).grid(row=4, column=2, padx=(8, 0))
        tk.Entry(form, textvariable=self.server_url_var, **entry_style).grid(row=5, column=1, columnspan=2, sticky="ew", pady=5)
        tk.Entry(form, textvariable=self.token_var, show="*", **entry_style).grid(row=6, column=1, sticky="ew", pady=5)
        tk.Label(form, textvariable=self.token_state_var, bg="#ffffff", fg="#7d8ca3", font=("Microsoft YaHei UI", 9)).grid(row=6, column=2, padx=(12, 0))
        tk.Entry(form, textvariable=self.database_key_var, show="*", **entry_style).grid(row=7, column=1, sticky="ew", pady=5)
        tk.Label(form, textvariable=self.database_key_state_var, bg="#ffffff", fg="#7d8ca3", font=("Microsoft YaHei UI", 9)).grid(row=7, column=2, padx=(12, 0))
        tk.Spinbox(form, from_=10, to=100, increment=5, textvariable=self.interval_var, width=8, relief="flat", bd=0, highlightthickness=1, highlightbackground="#d9e3ef", bg="#fbfdff", fg="#1e2b43", buttonbackground="#eef2f8", font=("Microsoft YaHei UI", 9)).grid(row=8, column=1, sticky="w", pady=5)
        tk.Checkbutton(form, text=_ui_text(r"\u9a8c\u8bc1 HTTPS \u8bc1\u4e66"), variable=self.verify_tls_var, bg="#ffffff", activebackground="#ffffff", fg="#526177", selectcolor="#e9ddff", font=("Microsoft YaHei UI", 9)).grid(row=9, column=1, sticky="w", pady=(10, 0))
        form.columnconfigure(1, weight=1)

        controls = tk.Frame(self.settings_tab, bg="#f8fbff")
        controls.pack(fill="x", pady=14)
        for text, command, color in ((_ui_text(r"\u751f\u6210 QQ \u89e3\u9501\u811a\u672c"), self.generate_unlock_script, "#8b5cf6"), ("\u81ea\u52a8\u53d1\u73b0 QQ \u76ee\u5f55", self.auto_detect, "#eef2f8"), ("\u4fdd\u5b58\u5e76\u8fc1\u79fb\u914d\u7f6e", self.save_settings, "#5b3b91"), ("\u6d4b\u8bd5\u670d\u52a1\u5668\u8fde\u63a5", self.test_connection, "#eef2f8"), ("\u91cd\u65b0\u68c0\u67e5\u6570\u636e\u5e93", self.inspect_databases, "#eef2f8")):
            tk.Button(controls, text=text, command=command, relief="flat", bd=0, padx=13, pady=9, bg=color, fg="#ffffff" if color == "#5b3b91" else "#526177", activebackground=color, font=("Microsoft YaHei UI", 9, "bold" if color == "#5b3b91" else "normal")).pack(side="left", padx=(0, 8))
        tk.Checkbutton(controls, text=_ui_text(r"\u5f00\u673a\u81ea\u52a8\u542f\u52a8"), variable=self.autostart_var, bg="#f8fbff", activebackground="#f8fbff", fg="#526177", selectcolor="#e9ddff", font=("Microsoft YaHei UI", 9)).pack(side="left", padx=(8, 0))

        help_text = (
            _ui_text(r"\u3010\u89e3\u6790\u5668\u6570\u636e\u76ee\u5f55\u3011\uff1a\u4fdd\u5b58\u6e38\u6807\u3001\u961f\u5217\u3001 DPAPI \u51ed\u636e\u3001\u5a92\u4f53\u6682\u5b58\u548c\u5feb\u7167\u3002\n")
            + _ui_text(r"\u3010API Token\u3011\uff1a\u7528\u4e8e\u8ba4\u8bc1\u91c7\u96c6\u5668\u5e76\u4e0a\u4f20\u6570\u636e\uff0c\u4e0d\u662f QQ \u5bc6\u7801\u3002\n")
            + _ui_text(r"\u3010QQ \u5b89\u88c5\u76ee\u5f55\u3011\uff1a\u4ec5\u7528\u4e8e\u751f\u6210\u89e3\u9501\u811a\u672c\uff0c\u811a\u672c\u7531\u7528\u6237\u624b\u52a8\u6267\u884c\uff0c\u4e0d\u4f1a\u81ea\u52a8\u4fee\u6539 QQ \u6570\u636e\u3002\n")
            + _ui_text(r"\u3010QQNT \u6570\u636e\u5e93\u5bc6\u94a5\u3011\uff1a\u7528\u4e8e\u89e3\u5bc6 nt_msg.db\uff0c\u586b\u5199 16 \u4f4d ASCII \u5bc6\u94a5\u3002\n")
            + _ui_text(r"\u3010\u8bfb\u53d6\u95f4\u9694\u3011\uff1a\u540e\u53f0\u6bcf\u9694 10\u2013100 \u79d2\u6267\u884c\u4e00\u6b21\u589e\u91cf\u8bfb\u53d6\u3002")
        )
        help_card = tk.Frame(self.settings_tab, bg="#f2f6fb", padx=16, pady=12)
        help_card.pack(fill="x")
        tk.Label(help_card, text=help_text, bg="#f2f6fb", fg="#7a879a", justify="left", anchor="w", wraplength=920, font=("Microsoft YaHei UI", 9)).pack(fill="x")

    def _build_logs(self) -> None:
        card = tk.Frame(self.logs_tab, bg="#ffffff", highlightthickness=1, highlightbackground="#dce6f2")
        card.pack(fill="both", expand=True)
        toolbar = tk.Frame(card, bg="#ffffff", padx=18, pady=14)
        toolbar.pack(fill="x")
        tk.Label(toolbar, text=_ui_text(r"\u6d3b\u52a8\u65f6\u95f4\u7ebf"), bg="#ffffff", fg="#17233d", font=("Microsoft YaHei UI", 13, "bold")).pack(side="left")
        tk.Label(toolbar, text=_ui_text(r"\u6700\u65b0\u8bb0\u5f55\u663e\u793a\u5728\u5e95\u90e8"), bg="#ffffff", fg="#8a97a9", font=("Microsoft YaHei UI", 9)).pack(side="right")
        log_body = tk.Frame(card, bg="#edf3fb", padx=8, pady=8)
        log_body.pack(fill="both", expand=True, padx=14, pady=(0, 14))
        self.log_text = tk.Text(log_body, height=20, state="disabled", wrap="word", font=("Cascadia Mono", 9), background="#edf3fb", foreground="#526177", insertbackground="#5b3b91", borderwidth=0, highlightthickness=0, padx=10, pady=10)
        scrollbar = ttk.Scrollbar(log_body, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    def _load_settings(self) -> None:
        settings = self.controller.load_settings()
        if settings is None:
            self._show_page("settings")
            self._append_log("首次启动：请填写 QQ 账号、数据目录、服务器地址和 Token。")
            return
        self.account_var.set(settings.account_id)
        self.device_var.set(settings.device_name)
        self.data_root_var.set(settings.data_root)
        self.qq_install_dir_var.set(settings.qq_install_dir)
        self.collector_data_dir_var.set(settings.collector_data_dir)
        self.server_url_var.set(settings.server_url)
        saved_interval = settings.sync_interval_seconds or settings.sync_interval_minutes * 60
        self.interval_var.set(saved_interval if 10 <= saved_interval <= 100 else 60)
        self.verify_tls_var.set(settings.verify_tls)
        self.autostart_var.set(settings.autostart)
        self.token_state_var.set("已安全保存" if self.controller.has_api_token() else "未保存")
        self.database_key_state_var.set("已安全保存" if self.controller.has_database_key() else "未保存")
        if not settings.data_root or not Path(settings.data_root).is_dir():
            self._show_page("settings")
            self._append_log("现有配置的数据目录不可用，请重新选择 QQNT 数据目录。")

    def _settings_value(self) -> GuiSettings:
        return GuiSettings(
            account_id=self.account_var.get(),
            device_name=self.device_var.get(),
            data_root=self.data_root_var.get(),
            qq_install_dir=self.qq_install_dir_var.get(),
            collector_data_dir=self.collector_data_dir_var.get(),
            server_url=self.server_url_var.get(),
            api_token=self.token_var.get(),
            qqnt_database_key=self.database_key_var.get(),
            sync_interval_minutes=max(1, int(self.interval_var.get()) // 60),
            sync_interval_seconds=int(self.interval_var.get()),
            autostart=bool(self.autostart_var.get()),
            verify_tls=bool(self.verify_tls_var.get()),
        )

    def _post_event(self, event: str, detail: str) -> None:
        self.events.put((event, detail))

    def _format_event(self, event: str, detail: str) -> str:
        parts = detail.split("|")
        if event == "scan.started" and len(parts) >= 2:
            label = _ui_text(r"\u5f00\u59cb\u89e3\u6790")
            return f"{label}\uff1a\u6a21\u5f0f {parts[0]}\uff0c\u53d1\u73b0 {parts[1]} \u4e2a\u6570\u636e\u5e93"
        if event == "scan.database":
            label = _ui_text(r"\u6b63\u5728\u89e3\u6790")
            return f"{label}\uff1a{Path(detail).name}"
        if event == "scan.progress" and len(parts) >= 3:
            label = _ui_text(r"\u89e3\u6790\u8fdb\u5ea6")
            return f"{label}\uff1a\u5df2\u5904\u7406 {parts[0]} \u6761\uff0c\u5df2\u5165\u961f {parts[1]} \u6761\uff0c\u89e3\u6790\u544a\u8b66 {parts[2]} \u6761"
        if event == "scan.completed" and len(parts) >= 4:
            label = _ui_text(r"\u89e3\u6790\u5b8c\u6210")
            return f"{label}\uff1a\u5904\u7406 {parts[0]} \u6761\uff0c\u5165\u961f {parts[1]} \u6761\uff0c\u89e3\u6790\u544a\u8b66 {parts[2]} \u6761\uff0c\u6570\u636e\u5e93\u95ee\u9898 {parts[3]} \u4e2a"
        if event == "upload.media.started":
            label = _ui_text(r"\u5f00\u59cb\u4e0a\u4f20\u5a92\u4f53")
            return f"{label}\uff1a\u672c\u6279 {detail} \u9879"
        if event == "upload.media.completed" and len(parts) >= 4:
            label = _ui_text(r"\u5a92\u4f53\u4e0a\u4f20\u5b8c\u6210")
            return f"{label}\uff1a\u5904\u7406 {parts[0]}\uff0c\u6210\u529f {parts[1]}\uff0c\u91cd\u8bd5 {parts[2]}\uff0c\u5931\u8d25 {parts[3]}"
        if event == "upload.messages.started" and len(parts) >= 2:
            label = _ui_text(r"\u5f00\u59cb\u4e0a\u4f20\u6d88\u606f")
            return f"{label}\uff1a\u6a21\u5f0f {parts[0]}\uff0c\u672c\u6279 {parts[1]} \u6761"
        if event == "upload.messages.completed" and len(parts) >= 5:
            label = _ui_text(r"\u6d88\u606f\u4e0a\u4f20\u5b8c\u6210")
            return f"{label}\uff1a\u5904\u7406 {parts[0]}\uff0c\u6210\u529f {parts[1]}\uff0c\u91cd\u8bd5 {parts[2]}\uff0c\u6b7b\u4fe1 {parts[3]}\uff0c\u670d\u52a1\u7aef\u5931\u8d25 {parts[4]}"
        return f"{event}: {detail}"

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
            formatted = self._format_event(event, detail)
            self._append_log(formatted)
            if event.startswith("scan."):
                self.activity_state = _ui_text(r"\u25cf \u6b63\u5728\u89e3\u6790")
                self.status_var.set(self.activity_state)
            elif event.startswith("upload."):
                self.activity_state = _ui_text(r"\u25cf \u6b63\u5728\u4e0a\u4f20")
                self.tray.set_status("uploading")
                self.status_var.set(self.activity_state)
            elif event == "scheduler.stopped":
                self.activity_state = ""
                self.tray.set_status("paused")
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
        self.activity_state = _ui_text(r"\u25cf \u540c\u6b65\u5931\u8d25")
        self._set_visual_status("error")
        self._append_log(f"操作失败：{detail}")
        if not quiet:
            messagebox.showerror("操作失败", detail)
        self._refresh_status()

    def _task_succeeded(self, result: Any, success: Callable[[Any], None] | None, *, quiet: bool) -> None:
        self.busy = False
        if isinstance(result, dict) and result.get("status") in {"completed", "partial"}:
            self.activity_state = _ui_text(r"\u25cf \u672c\u6b21\u540c\u6b65\u5df2\u5b8c\u6210")
        if success:
            success(result)
        elif not quiet:
            self._append_log("操作完成")
        self._refresh_status()

    def _update_progress_chart(self) -> None:
        if not hasattr(self, "progress_canvas"):
            return
        snapshot = getattr(self, "_last_snapshot", {}) or {}
        queues = snapshot.get("queues") or {}
        messages = queues.get("messages") or {}
        parsed = sum(int(value) for value in messages.values())
        uploaded = int(messages.get("completed", 0))
        pending = max(parsed - uploaded, 0)
        ratio = uploaded / parsed if parsed else 0
        canvas = self.progress_canvas
        canvas.delete("all")
        canvas.create_oval(28, 8, 182, 162, outline="#e6edf5", width=18)
        if parsed:
            canvas.create_arc(28, 8, 182, 162, start=90, extent=-360 * ratio, style="arc", outline="#22c55e", width=18)
            canvas.create_arc(28, 8, 182, 162, start=90, extent=-360 * min(1.0, (parsed - pending) / parsed), style="arc", outline="#8b5cf6", width=6)
        canvas.create_text(105, 78, text=f"{round(ratio * 100)}%", fill="#17233d", font=("Microsoft YaHei UI", 22, "bold"))
        canvas.create_text(105, 108, text=_ui_text(r"\u4e0a\u4f20\u5b8c\u6210"), fill="#718096", font=("Microsoft YaHei UI", 9))
        self.analyzed_var.set(str(parsed))
        self.uploaded_var.set(str(uploaded))
        if parsed:
            analyzed_label = _ui_text(r"\u5df2\u89e3\u6790")
            uploaded_label = _ui_text(r"\u6761\uff0c\u5df2\u4e0a\u4f20")
            item_label = _ui_text(r"\u6761")
            self.progress_detail_var.set(
                f"{analyzed_label} {parsed} {uploaded_label} {uploaded} {item_label}"
            )
        else:
            self.progress_detail_var.set(_ui_text(r"\u7b49\u5f85\u9996\u6b21\u540c\u6b65"))

    def _refresh_status(self) -> None:
        if self._refresh_after_id is not None:
            try:
                self.root.after_cancel(self._refresh_after_id)
            except tk.TclError:
                pass
            self._refresh_after_id = None
        try:
            snapshot = self.controller.status_snapshot()
            self._last_snapshot = snapshot
            self._update_progress_chart()
            configured = snapshot.get("configured", False)
            running = snapshot.get("running", False)
            if self.activity_state:
                self.status_var.set(self.activity_state)
            elif self.busy:
                self.status_var.set(_ui_text(r"\u25cf \u6b63\u5728\u6267\u884c"))
            else:
                self.status_var.set("\u25cf \u8fd0\u884c\u4e2d" if running else "\u25cb \u5df2\u6682\u505c" if configured else "\u672a\u914d\u7f6e")
                self._set_visual_status("uploading" if running else "paused")
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
            detail = str(exc).lower()
            if "locked" in detail or "busy" in detail or "database is locked" in detail:
                if not self.busy:
                    self.status_var.set(_ui_text(r"\u6b63\u5728\u8bfb\u53d6\u961f\u5217"))
                self._append_log(_ui_text(r"\u961f\u5217\u6b63\u5728\u66f4\u65b0\uff0c\u6682\u65f6\u4fdd\u7559\u4e0a\u6b21\u72b6\u6001"))
            else:
                self.status_var.set(_ui_text(r"\u914d\u7f6e\u9519\u8bef"))
                status_read_failed = _ui_text(r"\u72b6\u6001\u8bfb\u53d6\u5931\u8d25")
                self._append_log(f"{status_read_failed}：{exc}")
        self._refresh_after_id = self.root.after(3000, self._refresh_status)

    def _show_compatibility(self, report: Any) -> None:
        self.compatibility_var.set(report.message)
        self._append_log(f"数据库检测：{report.message}")

    def browse_data_root(self) -> None:
        selected = filedialog.askdirectory(title="选择 QQNT 数据目录")
        if selected:
            self.data_root_var.set(selected)

    def browse_qq_install_dir(self) -> None:
        selected = filedialog.askdirectory(title="选择 QQ 安装目录")
        if selected:
            self.qq_install_dir_var.set(selected)

    def browse_collector_data_dir(self) -> None:
        selected = filedialog.askdirectory(title=_ui_text(r"\u9009\u62e9\u89e3\u6790\u5668\u6570\u636e\u76ee\u5f55"))
        if selected:
            self.collector_data_dir_var.set(selected)

    def auto_detect(self) -> None:
        account_id = self.account_var.get()

        def success(value: str | None) -> None:
            if value:
                self.data_root_var.set(value)
                self._append_log(f"已发现 QQNT 数据目录：{value}")
            else:
                messagebox.showwarning("未发现", "未自动发现 QQNT 数据目录，请手动选择。")

        self._run_task(lambda: self.controller.auto_detect_data_root(account_id), success)

    def generate_unlock_script(self) -> None:
        default_path = Path.home() / "Desktop" / "chat-audit-qqnt-unlock.bat"
        output = filedialog.asksaveasfilename(
            title=_ui_text(r"\u4fdd\u5b58 QQ \u89e3\u9501\u811a\u672c"),
            initialdir=str(default_path.parent),
            initialfile=default_path.name,
            defaultextension=".bat",
            filetypes=[
                (_ui_text(r"\u4e00\u952e\u542f\u52a8\u811a\u672c"), "*.bat"),
                ("PowerShell", "*.ps1"),
                (_ui_text(r"\u6240\u6709\u6587\u4ef6"), "*.*"),
            ],
        )
        if not output:
            return

        def success(launcher: Path) -> None:
            powershell_script = launcher.with_suffix(".ps1")
            message = (
                _ui_text(r"\u5df2\u751f\u6210\u4e00\u952e\u542f\u52a8\u6587\u4ef6") + f"\uff1a{launcher}\n"
                + _ui_text(r"\u540c\u65f6\u751f\u6210 PowerShell \u811a\u672c") + f"\uff1a{powershell_script}\n\n"
                + _ui_text(r"\u53ef\u76f4\u63a5\u53cc\u51fb BAT \u8fd0\u884c\uff0c\u6216\u5728 PowerShell \u4e2d\u6267\u884c") + "\uff1a\n"
                + f"powershell.exe -NoProfile -ExecutionPolicy Bypass -File \"{powershell_script}\"\n\n"
                + _ui_text(r"\u8fd0\u884c\u524d\u8bf7\u5148\u5b8c\u5168\u9000\u51fa QQ\uff0c\u811a\u672c\u4e0d\u4f1a\u81ea\u52a8\u6267\u884c") + "\u3002"
            )
            messagebox.showinfo(_ui_text(r"\u811a\u672c\u5df2\u751f\u6210"), message)

        # Tk variables belong to the main thread; _run_task runs its callable on
        # a worker. Reading them inside the lambda touched Tk from that worker,
        # which is undefined behaviour rather than a reliable crash -- so the
        # values are captured here, exactly as auto_detect already does.
        data_root = self.data_root_var.get()
        qq_install_dir = self.qq_install_dir_var.get()
        self._run_task(
            lambda: self.controller.generate_unlock_script(data_root, qq_install_dir, output),
            success,
        )

    def save_settings(self) -> None:
        def success(_config: Any) -> None:
            self.token_var.set("")
            self.database_key_var.set("")
            self.token_state_var.set("已安全保存" if self.controller.has_api_token() else "未保存")
            self.database_key_state_var.set("已安全保存" if self.controller.has_database_key() else "未保存")
            self._append_log("配置已保存")
            self.inspect_databases()

        # Same reason: _settings_value() reads a dozen Tk variables, so it runs
        # on the main thread and the worker only sees the resulting value.
        settings_value = self._settings_value()
        self._run_task(lambda: self.controller.save_settings(settings_value), success)

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
        # stop() waits for the sync thread to wind down. Doing that here froze
        # the window for as long as the wait took.
        def success(stopped: bool) -> None:
            self._append_log("后台同步已停止" if stopped else "后台同步未运行或仍在结束当前操作")
            self._refresh_status()

        self._run_task(self.controller.stop, success)

    def sync_once(self) -> None:
        self._run_task(
            lambda: self.controller.run_once("incremental"),
            lambda result: self._append_log(f"同步完成：{result.get('status')}")
        )

    def initial_import(self) -> None:
        if not messagebox.askyesno("开始解析", "将读取一批历史消息并解析入队，随后按同步设置上传，是否继续？"):
            return
        self._run_task(
            lambda: self.controller.run_once("initial"),
            lambda result: self._append_log(f"解析批次完成：{result.get('status')}")
        )

    def repair_history(self) -> None:
        if not messagebox.askyesno(
            _ui_text(r"\u4fee\u590d\u5386\u53f2\u8d44\u6599"),
            _ui_text(r"\u5c06\u91cd\u65b0\u626b\u63cf\u672c\u5730 QQNT \u5168\u90e8\u5386\u53f2\u6d88\u606f\uff0c\u8865\u9f50\u7fa4\u540d\u3001\u6635\u79f0\u3001\u5934\u50cf\u5e76\u91cd\u65b0\u89e3\u6790\u53ef\u6062\u590d\u5185\u5bb9\u3002\n\n\u4e0d\u4f1a\u91cd\u590d\u521b\u5efa\u6d88\u606f\uff0c\u4f46\u53ef\u80fd\u9700\u8981\u8f83\u957f\u65f6\u95f4\uff0c\u662f\u5426\u7ee7\u7eed\uff1f"),
        ):
            return
        history_repair_label = _ui_text(r"\u5386\u53f2\u8d44\u6599\u4fee\u590d\u5b8c\u6210")
        self._run_task(
            lambda: self.controller.run_once("reconcile"),
            lambda result: self._append_log(f"{history_repair_label}：{result.get('status')}"),
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
    parser.add_argument("--start-sync", action="store_true", help="start background sync after the GUI initializes")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        with SingleInstance(args.config):
            app = CollectorTrayApp(args.config, start_hidden=args.start_hidden)
            if args.start_sync:
                app.root.after(750, app.start_sync)
            app.run()
    except AlreadyRunningError as exc:
        # windll exists only on Windows, so this last-resort message box turned
        # "another instance is running" into an AttributeError everywhere else.
        if hasattr(ctypes, "windll"):
            ctypes.windll.user32.MessageBoxW(None, str(exc), "Chat Audit QQ Collector", 0x40)
        else:
            print(str(exc), file=sys.stderr)
        return 1
    return 0


__all__ = ["CollectorTrayApp", "build_parser", "main"]
