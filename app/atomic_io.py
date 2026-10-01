"""Single source of truth for writing a file that must never be half-written.

Backups and restored media are read back by other processes -- the scheduler
lists ``auto-backup-*.json`` and hands the newest one to a restore, the media
routes serve whatever is on disk. A plain ``write_bytes`` is not atomic: a crash,
a full disk or a container stop partway through leaves a truncated file sitting
where a complete one is expected, and a truncated backup looks exactly like the
latest good one.

Writing to a temporary file in the same directory and then renaming it means a
reader sees either the previous content or the new content, never a prefix of
the new one. Same directory matters: ``os.replace`` is only atomic within a
filesystem.
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4


def atomic_write_bytes(path: Path, content: bytes) -> None:
    """Write ``content`` to ``path`` so readers never observe a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    try:
        temp_path.write_bytes(content)
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def atomic_write_text(path: Path, content: str, encoding: str = "utf-8") -> None:
    """Text counterpart of :func:`atomic_write_bytes`.

    Encoding is explicit rather than platform-default, so a file written on one
    host reads back the same on another.
    """
    atomic_write_bytes(path, content.encode(encoding))
