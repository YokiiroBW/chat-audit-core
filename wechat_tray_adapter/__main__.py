from __future__ import annotations

import sys

from wechat_tray_adapter.tray import run_tray
from wechat_tray_adapter.wcf_bridge import run_bridge


if __name__ == "__main__":
    if "--wcf-bridge" in sys.argv:
        raise SystemExit(run_bridge())
    run_tray()
