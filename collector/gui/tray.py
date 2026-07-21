from __future__ import annotations

from collections.abc import Callable

import pystray
from PIL import Image, ImageDraw


def create_tray_image(size: int = 64, status: str = "paused") -> Image.Image:
    colors = {
        "uploading": (34, 197, 94),
        "paused": (245, 158, 11),
        "analyzing": (139, 92, 246),
        "error": (239, 68, 68),
    }
    lamp = colors.get(status, colors["paused"])
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    center = size // 2
    radius = max(10, size // 4)
    draw.ellipse((5, 5, size - 5, size - 5), fill="#17233d", outline="#ffffff", width=2)
    draw.ellipse((center - radius - 4, center - radius - 4, center + radius + 4, center + radius + 4), fill=(*lamp, 65))
    draw.ellipse((center - radius, center - radius, center + radius, center + radius), fill=lamp, outline="#ffffff", width=2)
    draw.ellipse((center - radius // 3, center - radius // 2, center + radius // 5, center - radius // 7), fill="#ffffff")
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
        self.status = "paused"
        self.icon = pystray.Icon(
            "chat-audit-qq-collector",
            create_tray_image(status=self.status),
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

    def set_status(self, status: str) -> None:
        if status not in {"uploading", "paused", "analyzing", "error"}:
            status = "paused"
        self.status = status
        self.icon.icon = create_tray_image(status=status)
        labels = {
            "uploading": "\u6b63\u5728\u4e0a\u4f20",
            "paused": "\u5df2\u6682\u505c",
            "analyzing": "\u6b63\u5728\u5206\u6790",
            "error": "\u540c\u6b65\u9519\u8bef",
        }
        self.icon.title = f"Chat Audit QQ Collector · {labels[status]}"

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
