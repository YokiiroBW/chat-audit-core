from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Mapping

from wechat_tray_adapter.config import AdapterConfig, default_config_path


EDITABLE_FIELDS = (
    "nas_url",
    "token",
    "account_id",
    "account_name",
    "auto_download_media",
    "autostart",
    "paused",
    "retry_interval_seconds",
)


def config_to_editable_dict(config: AdapterConfig) -> dict[str, Any]:
    return {field: getattr(config, field) for field in EDITABLE_FIELDS}


def write_config(path: str | Path, values: Mapping[str, Any]) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {field: values.get(field) for field in EDITABLE_FIELDS if field in values}
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


def run_config_wizard(path: str | Path | None = None) -> None:
    config_path = Path(path) if path is not None else default_config_path()
    config = AdapterConfig.load(config_path) if config_path.exists() else AdapterConfig.default()
    try:
        import tkinter as tk
        from tkinter import messagebox
    except ImportError:
        write_config(config_path, config_to_editable_dict(config))
        open_config_file(config_path)
        return

    root = tk.Tk()
    root.title("Chat Audit 微信采集配置")
    root.resizable(False, False)

    entries: dict[str, Any] = {}
    values = config_to_editable_dict(config)
    for row, field in enumerate(EDITABLE_FIELDS):
        label = tk.Label(root, text=field)
        label.grid(row=row, column=0, sticky="w", padx=12, pady=6)
        if isinstance(values[field], bool):
            variable = tk.BooleanVar(value=bool(values[field]))
            widget = tk.Checkbutton(root, variable=variable)
            widget.grid(row=row, column=1, sticky="w", padx=12, pady=6)
            entries[field] = variable
        else:
            variable = tk.StringVar(value="" if values[field] is None else str(values[field]))
            widget = tk.Entry(root, textvariable=variable, width=48, show="*" if field == "token" else "")
            widget.grid(row=row, column=1, sticky="ew", padx=12, pady=6)
            entries[field] = variable

    def save() -> None:
        payload: dict[str, Any] = {}
        for field, variable in entries.items():
            value = variable.get()
            if field == "retry_interval_seconds":
                value = int(value)
            payload[field] = value
        write_config(config_path, payload)
        messagebox.showinfo("Chat Audit", f"配置已保存到 {config_path}")
        root.destroy()

    button_frame = tk.Frame(root)
    button_frame.grid(row=len(EDITABLE_FIELDS), column=0, columnspan=2, sticky="e", padx=12, pady=12)
    tk.Button(button_frame, text="保存", command=save).pack(side="right", padx=4)
    tk.Button(button_frame, text="取消", command=root.destroy).pack(side="right", padx=4)
    root.mainloop()


def open_config_file(path: str | Path) -> None:
    target = Path(path)
    if sys.platform == "win32":
        os.startfile(str(target))  # type: ignore[attr-defined]
        return
    subprocess.Popen(["xdg-open", str(target)])
