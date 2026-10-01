from __future__ import annotations

import hashlib
import msvcrt
from pathlib import Path


class AlreadyRunningError(RuntimeError):
    pass


class SingleInstance:
    def __init__(self, config_path: str | Path) -> None:
        resolved = Path(config_path).expanduser().resolve()
        identity = hashlib.sha256(str(resolved).casefold().encode("utf-8")).hexdigest()[:16]
        self.path = resolved.parent / f"collector-gui-{identity}.lock"
        self.file = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        if self.file.seek(0, 2) == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            self.file.close()
            self.file = None
            raise AlreadyRunningError("Collector 托盘软件已经在运行") from exc

    def release(self) -> None:
        if self.file is None:
            return
        try:
            self.file.seek(0)
            msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            self.file.close()
            self.file = None

    def __enter__(self) -> "SingleInstance":
        self.acquire()
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.release()


__all__ = ["AlreadyRunningError", "SingleInstance"]
