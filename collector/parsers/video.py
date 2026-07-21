from typing import Any

from collector.media.resolver import MediaResolver
from collector.parsers.base import ElementResult
from collector.parsers.common_media import parse_media_element


def parse_video_element(element: dict[str, Any], *, ordinal: int, resolver: MediaResolver) -> ElementResult:
    element = dict(element)
    element.setdefault("element_type", "videoElement")
    return parse_media_element(element, ordinal=ordinal, media_type="video", cq_kind="video", resolver=resolver)
