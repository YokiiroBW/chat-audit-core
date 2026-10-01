from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from collector.media.checksum import calculate_checksums


@dataclass(frozen=True)
class IndexedMediaFile:
    path: Path
    file_name: str
    size: int
    modified_ns: int


class MediaFileIndex:
    def __init__(self, roots: list[str | Path] | tuple[str | Path, ...], *, max_files: int = 200_000) -> None:
        self.roots = tuple(Path(root).expanduser().resolve() for root in roots)
        self.max_files = max_files
        self._by_name: dict[str, list[IndexedMediaFile]] = {}
        self._md5_cache: dict[Path, str] = {}

    def build(self) -> int:
        self._by_name.clear()
        count = 0
        for root in self.roots:
            if not root.is_dir():
                continue
            for current_root, directories, files in os.walk(root):
                directories[:] = [name for name in directories if name.lower() not in {"temp", "tmp"}]
                directories.sort(key=str.casefold)
                for file_name in sorted(files, key=str.casefold):
                    if count >= self.max_files:
                        return count
                    path = Path(current_root) / file_name
                    try:
                        stat = path.stat()
                    except OSError:
                        continue
                    if stat.st_size <= 0:
                        continue
                    entry = IndexedMediaFile(path.resolve(), file_name, stat.st_size, stat.st_mtime_ns)
                    self._by_name.setdefault(file_name.lower(), []).append(entry)
                    count += 1
        return count

    def find(
        self,
        *,
        file_name: str | None = None,
        declared_size: int | None = None,
        source_md5: str | None = None,
    ) -> Path | None:
        normalized_md5 = (source_md5 or "").lower()
        if not file_name and not normalized_md5:
            # Size alone does not identify a file. Scanning every indexed file
            # for one within 5% of a declared size binds whatever happens to be
            # close, and the collector then archives that file as the message's
            # media. A name or a checksum is the minimum evidence.
            return None
        candidates = self._by_name.get((file_name or "").lower(), []) if file_name else [
            item for values in self._by_name.values() for item in values
        ]
        if len(candidates) > 1 and declared_size is None and not normalized_md5:
            return None
        for candidate in candidates:
            tolerance = max(64 * 1024, int((declared_size or candidate.size) * 0.05))
            if declared_size is not None and abs(candidate.size - declared_size) > tolerance:
                continue
            if normalized_md5:
                digest = self._md5_cache.get(candidate.path)
                if digest is None:
                    digest = calculate_checksums(candidate.path).md5
                    self._md5_cache[candidate.path] = digest
                if digest.lower() != normalized_md5:
                    continue
            return candidate.path
        return None
