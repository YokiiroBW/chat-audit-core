from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from collector.media.checksum import calculate_checksums
from collector.media.indexer import MediaFileIndex
from collector.media.paths import within_any


@dataclass(frozen=True)
class ResolvedMedia:
    full_file: Path | None
    thumbnail_file: Path | None
    explicit_local_path_missing: bool
    failure_code: str | None = None


class MediaResolver:
    FULL_PATH_FIELDS = ("local_path", "files_in_chat_path", "rich_media_path")
    THUMBNAIL_PATH_FIELDS = ("thumbnail_path", "thumb_path", "preview_path")

    def __init__(self, index: MediaFileIndex | None = None, allowed_roots: tuple[Path, ...] = ()) -> None:
        self.index = index
        # Paths in a QQNT row are data. Without roots to check them against, a
        # row can name any file on the disk and the collector will read and
        # upload it. Empty means unrestricted, which only the parser's own
        # default resolver uses; the scanner always passes the QQ data root.
        self.allowed_roots = allowed_roots

    def _valid_file(
        self,
        value: Any,
        *,
        declared_size: int | None = None,
        source_md5: str | None = None,
    ) -> Path | None:
        if not isinstance(value, str) or not value:
            return None
        path = Path(value).expanduser()
        try:
            resolved = path.resolve()
            size = resolved.stat().st_size
        except OSError:
            return None
        if not resolved.is_file() or size <= 0:
            return None
        if self.allowed_roots and not within_any(self.allowed_roots, resolved):
            return None
        if declared_size is not None:
            tolerance = max(64 * 1024, int(declared_size * 0.05))
            if abs(size - declared_size) > tolerance:
                return None
        if source_md5 and calculate_checksums(resolved).md5.lower() != source_md5.lower():
            return None
        return resolved

    def resolve(self, element: dict[str, Any]) -> ResolvedMedia:
        declared_size = element.get("file_size")
        declared_size_value = int(declared_size) if isinstance(declared_size, int) and declared_size >= 0 else None
        source_md5 = element.get("md5") if isinstance(element.get("md5"), str) else None
        explicit_paths = [element.get(field) for field in self.FULL_PATH_FIELDS if element.get(field)]
        full_file = None
        for candidate in explicit_paths:
            full_file = self._valid_file(candidate, declared_size=declared_size_value, source_md5=source_md5)
            if full_file:
                break
        if full_file is None and self.index is not None:
            file_name = element.get("file_name") if isinstance(element.get("file_name"), str) else None
            alternate_name = element.get("name") if isinstance(element.get("name"), str) else None
            has_media_identity = bool(
                declared_size_value
                or source_md5
                or isinstance(element.get("sha1"), str)
                or isinstance(element.get("uuid"), str)
            )
            if file_name or alternate_name or has_media_identity:
                full_file = self.index.find(
                    file_name=file_name or alternate_name,
                    declared_size=declared_size_value,
                    source_md5=source_md5,
                )

        thumbnail_file = None
        for field in self.THUMBNAIL_PATH_FIELDS:
            thumbnail_file = self._valid_file(element.get(field))
            if thumbnail_file:
                break
        explicit_missing = bool(explicit_paths) and full_file is None
        failure_code = None
        if explicit_missing:
            failure_code = "MEDIA_SOURCE_MISSING"
        return ResolvedMedia(full_file, thumbnail_file, explicit_missing, failure_code)
