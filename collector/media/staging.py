from __future__ import annotations

import hashlib
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path


class StagingError(RuntimeError):
    pass


class StagingFullError(StagingError):
    pass


@dataclass(frozen=True)
class StagedFile:
    path: Path
    file_name: str
    file_size: int
    file_hash: str
    content_sha256: str


class MediaStagingStore:
    def __init__(self, root: str | Path, *, max_bytes: int) -> None:
        if max_bytes <= 0:
            raise ValueError("staging max_bytes must be positive")
        self.root = Path(root).expanduser().resolve()
        self.max_bytes = max_bytes
        self.root.mkdir(parents=True, exist_ok=True)

    def used_bytes(self) -> int:
        return sum(path.stat().st_size for path in self.root.rglob("*") if path.is_file())

    @staticmethod
    def _safe_extension(path: Path) -> str:
        suffix = path.suffix.lower()
        return suffix if re.fullmatch(r"\.[a-z0-9]{1,12}", suffix) else ".bin"

    def stage(self, source_path: str | Path) -> StagedFile:
        source = Path(source_path).expanduser().resolve()
        if not source.is_file():
            raise StagingError(f"media source file does not exist: {source.name}")
        source_size = source.stat().st_size
        if source_size <= 0:
            raise StagingError("zero-byte media cannot be staged")
        if self.used_bytes() + source_size > self.max_bytes:
            raise StagingFullError("MEDIA_STAGING_FULL")

        temporary = self.root / f".{uuid.uuid4().hex}.tmp"
        md5 = hashlib.md5()
        sha256 = hashlib.sha256()
        copied = 0
        try:
            with source.open("rb") as input_file, temporary.open("xb") as output_file:
                while chunk := input_file.read(1024 * 1024):
                    output_file.write(chunk)
                    md5.update(chunk)
                    sha256.update(chunk)
                    copied += len(chunk)
                output_file.flush()
                os.fsync(output_file.fileno())
            if copied != source_size or copied <= 0:
                raise StagingError("staged media size mismatch")
            destination = self.root / f"{sha256.hexdigest()}{self._safe_extension(source)}"
            if destination.exists():
                if destination.stat().st_size != copied:
                    raise StagingError("staging hash collision with a different file size")
                temporary.unlink()
            else:
                os.replace(temporary, destination)
            return StagedFile(
                path=destination,
                file_name=source.name,
                file_size=copied,
                file_hash=md5.hexdigest(),
                content_sha256=sha256.hexdigest(),
            )
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    def stage_bytes(self, content: bytes, *, file_name: str) -> StagedFile:
        if not content:
            raise StagingError("zero-byte media cannot be staged")
        if self.used_bytes() + len(content) > self.max_bytes:
            raise StagingFullError("MEDIA_STAGING_FULL")
        md5 = hashlib.md5(content).hexdigest()
        sha256 = hashlib.sha256(content).hexdigest()
        extension = self._safe_extension(Path(file_name))
        destination = self.root / f"{sha256}{extension}"
        if not destination.exists():
            temporary = self.root / f".{uuid.uuid4().hex}.tmp"
            try:
                with temporary.open("xb") as file:
                    file.write(content)
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(temporary, destination)
            except Exception:
                temporary.unlink(missing_ok=True)
                raise
        elif destination.stat().st_size != len(content):
            raise StagingError("staging hash collision with a different file size")
        return StagedFile(destination, file_name, len(content), md5, sha256)

    def import_existing(self, staged_path: str | Path) -> StagedFile:
        path = Path(staged_path).expanduser().resolve()
        if self.root != path and self.root not in path.parents:
            raise StagingError("staged path escapes the collector staging directory")
        if not path.is_file() or path.stat().st_size <= 0:
            raise StagingError("staged media is missing or empty")
        md5 = hashlib.md5()
        sha256 = hashlib.sha256()
        with path.open("rb") as file:
            while chunk := file.read(1024 * 1024):
                md5.update(chunk)
                sha256.update(chunk)
        return StagedFile(path, path.name, path.stat().st_size, md5.hexdigest(), sha256.hexdigest())

    def delete_confirmed(self, staged_path: str | Path, *, minimum_age_seconds: int = 86400) -> bool:
        path = Path(staged_path).expanduser().resolve()
        if self.root != path and self.root not in path.parents:
            raise StagingError("refusing to delete outside the staging directory")
        if not path.exists():
            return False
        if time.time() - path.stat().st_mtime < minimum_age_seconds:
            return False
        path.unlink()
        return True

    def clear_temporary_files(self, *, older_than_seconds: int = 3600) -> int:
        removed = 0
        cutoff = time.time() - older_than_seconds
        for path in self.root.glob(".*.tmp"):
            if path.is_file() and path.stat().st_mtime <= cutoff:
                path.unlink()
                removed += 1
        return removed
