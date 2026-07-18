from __future__ import annotations

from collections.abc import Callable

import pystray
from PIL import Image, ImageDraw


def create_tray_image(size: int = 64) -> Image.Image:
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((4, 4, size - 4, size - 4), radius=14, fill="#2563eb")
    draw.ellipse((15, 15, 29, 29), fill="white")
    draw.ellipse((35, 15, 49, 29), fill="white")
    draw.rounded_rectangle((15, 35, 49, 44), radius=4, fill="white")
    return image


class TrayManager:
    def __init__(
        self,
        *,
        show: Callable[[], None],
        start: Callable[[], None],
        stop: Callable[[], None],
        sync_once: Callable[[], None],
        diagnostics: Callable[[], None],
        exit_app: Callable[[], None],
    ) -> None:
        self.icon = pystray.Icon(
            "chat-audit-qq-collector",
            create_tray_image(),
            "Chat Audit QQ Collector",
            pystray.Menu(
                pystray.MenuItem("打开窗口", lambda _icon, _item: show(), default=True),
                pystray.MenuItem("开始同步", lambda _icon, _item: start()),
                pystray.MenuItem("暂停同步", lambda _icon, _item: stop()),
                pystray.MenuItem("立即同步", lambda _icon, _item: sync_once()),
                pystray.MenuItem("生成诊断包", lambda _icon, _item: diagnostics()),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("退出", lambda _icon, _item: exit_app()),
            ),
        )

    def start(self) -> None:
        self.icon.run_detached()

    def stop(self) -> None:
        self.icon.stop()

    def notify(self, message: str, title: str = "Chat Audit QQ Collector") -> None:
        try:
            self.icon.notify(message, title)
        except Exception:
            pass


__all__ = ["TrayManager", "create_tray_image"]
