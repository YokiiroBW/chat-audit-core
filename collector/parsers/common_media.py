from __future__ import annotations

from pathlib import Path
from typing import Any

from collector.media.classifier import classify_media
from collector.media.resolver import MediaResolver
from collector.parsers.base import ElementResult, ParsedMediaUpload
from collector.parsers.text import cq_escape


def parse_media_element(
    element: dict[str, Any],
    *,
    ordinal: int,
    media_type: str,
    cq_kind: str,
    resolver: MediaResolver,
) -> ElementResult:
    resolved = resolver.resolve(element)
    classification = classify_media(element, resolved, media_type)
    file_name = str(element.get("file_name") or element.get("name") or f"{media_type}-{ordinal}")
    reference = {
        "ordinal": ordinal,
        "media_type": media_type,
        "source_state": classification.source_state,
        "archive_state": classification.archive_state,
        "file_name": file_name,
        "file_ext": Path(file_name).suffix.lstrip(".") or None,
        "declared_file_size": element.get("file_size") if isinstance(element.get("file_size"), int) else None,
        "source_md5": element.get("md5") if isinstance(element.get("md5"), str) else None,
        "source_sha1": element.get("sha1") if isinstance(element.get("sha1"), str) else None,
        "source_uuid": element.get("uuid") if isinstance(element.get("uuid"), str) else None,
        "duration_ms": element.get("duration_ms") if isinstance(element.get("duration_ms"), int) else None,
        "width": element.get("width") if isinstance(element.get("width"), int) else None,
        "height": element.get("height") if isinstance(element.get("height"), int) else None,
        "failure_code": classification.failure_code,
        "metadata": {"element_type": element.get("element_type") or element.get("type") or media_type},
    }
    uploads: list[ParsedMediaUpload] = []
    if resolved.full_file is not None:
        uploads.append(ParsedMediaUpload(ordinal, "asset", media_type, resolved.full_file, file_name))
    elif resolved.thumbnail_file is not None:
        thumbnail_name = str(element.get("thumbnail_name") or resolved.thumbnail_file.name)
        uploads.append(ParsedMediaUpload(ordinal, "thumbnail", "image", resolved.thumbnail_file, thumbnail_name))
    return ElementResult(
        text=f"[CQ:{cq_kind},file={cq_escape(file_name)}]",
        media_reference=reference,
        uploads=uploads,
    )
