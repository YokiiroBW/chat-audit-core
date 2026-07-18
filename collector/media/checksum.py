from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class FileChecksums:
    md5: str
    sha1: str
    sha256: str
    size: int


def calculate_checksums(path: str | Path) -> FileChecksums:
    file_path = Path(path)
    md5 = hashlib.md5()
    sha1 = hashlib.sha1()
    sha256 = hashlib.sha256()
    size = 0
    with file_path.open("rb") as file:
        while chunk := file.read(1024 * 1024):
            md5.update(chunk)
            sha1.update(chunk)
            sha256.update(chunk)
            size += len(chunk)
    return FileChecksums(md5.hexdigest(), sha1.hexdigest(), sha256.hexdigest(), size)
