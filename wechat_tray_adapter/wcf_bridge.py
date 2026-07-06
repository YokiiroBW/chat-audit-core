from __future__ import annotations

import json
from queue import Empty
import sys
import time
from typing import Any


def wxmsg_to_dict(message: Any, self_wxid: str | None = None) -> dict[str, Any]:
    return {
        "self_wxid": self_wxid,
        "type": getattr(message, "type", None),
        "id": getattr(message, "id", None),
        "ts": getattr(message, "ts", None),
        "sign": getattr(message, "sign", None),
        "xml": getattr(message, "xml", None),
        "sender": getattr(message, "sender", None),
        "roomid": getattr(message, "roomid", None),
        "content": getattr(message, "content", None),
        "thumb": getattr(message, "thumb", None),
        "extra": getattr(message, "extra", None),
        "from_self": bool(message.from_self()) if hasattr(message, "from_self") else False,
        "from_group": bool(message.from_group()) if hasattr(message, "from_group") else False,
    }


def run_bridge() -> int:
    from wcferry import Wcf

    wcf = Wcf(debug=False, block=True)
    self_wxid = wcf.get_self_wxid() if hasattr(wcf, "get_self_wxid") else getattr(wcf, "self_wxid", None)
    if not wcf.enable_receiving_msg():
        print(json.dumps({"event": "error", "detail": "enable_receiving_msg failed"}), file=sys.stderr, flush=True)
        return 2

    print(json.dumps({"event": "ready", "self_wxid": self_wxid}, ensure_ascii=False), flush=True)
    while True:
        try:
            message = wcf.get_msg(block=True)
        except Empty:
            time.sleep(0.1)
            continue
        print(json.dumps(wxmsg_to_dict(message, self_wxid=self_wxid), ensure_ascii=False, separators=(",", ":")), flush=True)


if __name__ == "__main__":
    raise SystemExit(run_bridge())
