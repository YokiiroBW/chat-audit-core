from __future__ import annotations

import logging
from pathlib import Path
import threading
import time
import webbrowser
from typing import Any, Callable

from wechat_tray_adapter.client import NasClient
from wechat_tray_adapter.config import AdapterConfig
from wechat_tray_adapter.config_wizard import run_config_wizard
from wechat_tray_adapter.queue import PendingEventQueue
from wechat_tray_adapter.worker import SyncWorker


def configure_logging(config: AdapterConfig) -> None:
    config.ensure_dirs()
    log_path = (config.log_dir or Path.cwd()) / "wechat-tray.log"
    logging.basicConfig(
        filename=log_path,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


def load_wcf_factory() -> Callable[[], Any]:
    try:
        from wcferry import Wcf  # type: ignore
    except ImportError as exc:
        raise RuntimeError("wcferry is not installed") from exc
    return Wcf


class WechatTrayApp:
    def __init__(self, config: AdapterConfig):
        self.config = config
        if config.queue_db is None:
            raise ValueError("queue_db is required")
        self.queue = PendingEventQueue(config.queue_db)
        self.client = NasClient(config)
        self.worker = SyncWorker(config, self.client, self.queue)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start_worker(self) -> None:
        self._thread = threading.Thread(target=self._run_wcf_loop, name="wechat-wcf-loop", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def open_nas(self) -> None:
        webbrowser.open(self.config.normalized_nas_url)

    def flush_pending_once(self) -> int:
        return self.worker.flush_pending()

    def _run_wcf_loop(self) -> None:
        try:
            wcf_factory = load_wcf_factory()
            wcf = wcf_factory()
            if hasattr(wcf, "enable_receiving_msg"):
                wcf.enable_receiving_msg()
            logging.info("WeChatFerry receiving loop started")
            while not self._stop.is_set():
                raw = self._receive_message(wcf)
                if raw is not None:
                    self.worker.handle_wcf_message(raw)
                self.worker.flush_pending(limit=20)
        except Exception:
            logging.exception("WeChat tray worker stopped")

    def _receive_message(self, wcf: Any) -> dict[str, Any] | None:
        if hasattr(wcf, "get_msg"):
            message = wcf.get_msg()
        elif hasattr(wcf, "get_message"):
            message = wcf.get_message()
        else:
            time.sleep(self.config.retry_interval_seconds)
            return None
        if message is None:
            time.sleep(0.2)
            return None
        if isinstance(message, dict):
            return message
        if hasattr(message, "__dict__"):
            return dict(message.__dict__)
        return None


def run_tray(config: AdapterConfig | None = None) -> None:
    active_config = config or AdapterConfig.load()
    configure_logging(active_config)
    app = WechatTrayApp(active_config)
    app.start_worker()

    try:
        import pystray  # type: ignore
        from PIL import Image, ImageDraw  # type: ignore
    except ImportError as exc:
        logging.exception("pystray and Pillow are required for tray mode")
        raise RuntimeError("pystray and Pillow are required for tray mode") from exc

    image = Image.new("RGB", (64, 64), "#1f2937")
    draw = ImageDraw.Draw(image)
    draw.rectangle((14, 14, 50, 50), fill="#22c55e")
    draw.text((23, 23), "W", fill="#111827")

    def on_open(_: Any) -> None:
        app.open_nas()

    def on_flush(_: Any) -> None:
        app.flush_pending_once()

    def on_config(_: Any) -> None:
        run_config_wizard()

    def on_quit(icon: Any) -> None:
        app.stop()
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("打开 NAS 控制台", on_open),
        pystray.MenuItem("配置", on_config),
        pystray.MenuItem("立即补发队列", on_flush),
        pystray.MenuItem("退出", on_quit),
    )
    icon = pystray.Icon("chat-audit-wechat", image, "Chat Audit 微信采集", menu)
    icon.run()
