import html
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings


@dataclass(frozen=True)
class CQMediaSegment:
    raw: str
    media_type: str
    url: str
    ext: str


_CQ_PATTERN = re.compile(r"\[CQ:(?P<kind>\w+),(?P<params>[^\]]+)\]")
_SUPPORTED_MEDIA_TYPES = {
    "image": "image",
    "record": "voice",
    "video": "video",
}


def _parse_cq_params(params: str) -> dict[str, str]:
    pairs = []
    for item in params.split(","):
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        pairs.append((key, unquote(html.unescape(value))))
    return dict(pairs)


def _guess_ext(url: str, file_name: str | None, media_type: str) -> str:
    # Prefer URL suffix because NapCat file names may be cache keys such as
    # `abc.image`, while the URL usually carries the real downloadable format.
    url_suffix = Path(urlparse(url).path).suffix.lstrip(".").lower()
    if url_suffix:
        return url_suffix

    source = file_name or ""
    file_suffix = Path(source).suffix.lstrip(".").lower()
    if file_suffix:
        return file_suffix

    defaults = {"image": "jpg", "voice": "silk", "video": "mp4"}
    return defaults[media_type]


def parse_cq_media_segments(raw_message: str) -> list[CQMediaSegment]:
    segments: list[CQMediaSegment] = []
    for match in _CQ_PATTERN.finditer(raw_message):
        cq_type = match.group("kind")
        media_type = _SUPPORTED_MEDIA_TYPES.get(cq_type)
        if media_type is None:
            continue

        params = _parse_cq_params(match.group("params"))
        url = params.get("url")
        if not url:
            continue

        file_name = params.get("file")
        segments.append(
            CQMediaSegment(
                raw=match.group(0),
                media_type=media_type,
                url=url,
                ext=_guess_ext(url, file_name, media_type),
            )
        )
    return segments


class MediaService:
    @staticmethod
    async def rewrite_cq_media_to_local_paths(
        db: AsyncSession,
        raw_message: str,
        http_client: Any | None = None,
        storage_root: str | Path | None = None,
        public_prefix: str | None = None,
        max_bytes: int | None = None,
    ) -> str:
        from app.services.message_service import MessageService

        segments = parse_cq_media_segments(raw_message)
        if not segments:
            return raw_message

        media_max_bytes = max_bytes if max_bytes is not None else get_settings().media_max_bytes
        owns_client = http_client is None
        client = http_client or httpx.AsyncClient()
        rewritten = raw_message
        try:
            for segment in segments:
                try:
                    response = await client.get(segment.url)
                except httpx.HTTPError:
                    continue
                if response.status_code >= 400:
                    continue
                content_length = response.headers.get("content-length")
                if content_length is not None and content_length.isdigit() and int(content_length) > media_max_bytes:
                    continue
                if len(response.content) > media_max_bytes:
                    continue
                local_path = await MessageService.save_media_asset(
                    db,
                    file_content=response.content,
                    file_type=segment.media_type,
                    ext=segment.ext,
                    storage_root=storage_root,
                    public_prefix=public_prefix,
                )
                rewritten = rewritten.replace(segment.raw, local_path, 1)
        finally:
            if owns_client:
                await client.aclose()

        return rewritten
