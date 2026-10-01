from typing import Any

from collector.media.resolver import MediaResolver
from collector.parsers.base import ElementResult
from collector.parsers.common_media import parse_media_element


def parse_image_element(element: dict[str, Any], *, ordinal: int, resolver: MediaResolver) -> ElementResult:
    return parse_media_element(element, ordinal=ordinal, media_type="image", cq_kind="image", resolver=resolver)
