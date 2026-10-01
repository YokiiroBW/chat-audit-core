from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from collector.media.resolver import ResolvedMedia


@dataclass(frozen=True)
class MediaClassification:
    source_state: str
    archive_state: str
    failure_code: str | None = None


def has_media_identity(element: dict[str, Any], media_type: str) -> bool:
    return bool(
        media_type
        or element.get("element_type")
        or element.get("uuid")
        or element.get("md5")
        or element.get("duration_ms")
        or element.get("file_size")
        or element.get("thumbnail_path")
    )


def classify_media(element: dict[str, Any], resolved: ResolvedMedia, media_type: str) -> MediaClassification:
    if resolved.full_file is not None:
        return MediaClassification("downloaded", "complete")
    thumbnail_state = "thumbnail_only" if resolved.thumbnail_file is not None else "metadata_only"
    if element.get("indicates_downloaded_before") or resolved.explicit_local_path_missing:
        return MediaClassification("missing", thumbnail_state, resolved.failure_code or "MEDIA_SOURCE_MISSING")
    if has_media_identity(element, media_type):
        return MediaClassification("not_downloaded", thumbnail_state, "MEDIA_LOCAL_NOT_DOWNLOADED")
    return MediaClassification("unknown", "failed", "MESSAGE_PARSE_FAILED")
