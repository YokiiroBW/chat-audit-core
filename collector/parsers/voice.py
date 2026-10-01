from typing import Any

from collector.media.resolver import MediaResolver
from collector.parsers.base import ElementResult
from collector.parsers.common_media import parse_media_element


def parse_voice_element(element: dict[str, Any], *, ordinal: int, resolver: MediaResolver) -> ElementResult:
    result = parse_media_element(element, ordinal=ordinal, media_type="voice", cq_kind="record", resolver=resolver)
    if element.get("transcript"):
        result.media_reference["metadata"]["transcript"] = str(element["transcript"])
    if element.get("waveform"):
        result.media_reference["metadata"]["waveform"] = element["waveform"]
    return result
