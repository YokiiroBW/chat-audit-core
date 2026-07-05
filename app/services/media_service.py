import html
import json
import re
from copy import deepcopy
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
    "file": "file",
}
_URL_PATTERN = re.compile(r"https?://[^\s\"'<>\\\]]+")
_CARD_MEDIA_KEYS = {
    "audio",
    "avatar",
    "cover",
    "coverurl",
    "icon",
    "image",
    "imageurl",
    "pic",
    "picture",
    "preview",
    "previewurl",
    "src",
    "thumb",
    "thumbnail",
}
_CARD_PAGE_URL_KEYS = {
    "jumpurl",
    "link",
    "pageurl",
    "qqdocurl",
    "shareurl",
    "targeturl",
    "url",
}
_KNOWN_MEDIA_HOST_PARTS = (
    "gchat.qpic.cn",
    "multimedia.nt.qq.com.cn",
    "p.qlogo.cn",
    "q1.qlogo.cn",
    "qq.ugcimg.cn",
    "thirdqq.qlogo.cn",
)
_MEDIA_EXTENSIONS = {
    "amr",
    "ape",
    "avi",
    "bmp",
    "flac",
    "gif",
    "jpeg",
    "jpg",
    "m4a",
    "mkv",
    "mov",
    "mp3",
    "mp4",
    "ogg",
    "png",
    "silk",
    "wav",
    "webm",
    "webp",
}


def _parse_cq_params(params: str) -> dict[str, str]:
    pairs = []
    for item in params.split(","):
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        pairs.append((key, unquote(html.unescape(value))))
    return dict(pairs)


def _cq_escape_param(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace(",", "&#44;")
        .replace("[", "&#91;")
        .replace("]", "&#93;")
    )


def _build_cq_segment(kind: str, params: dict[str, str]) -> str:
    return f"[CQ:{kind}," + ",".join(f"{key}={_cq_escape_param(str(value))}" for key, value in params.items()) + "]"


def _guess_ext(url: str, file_name: str | None, media_type: str) -> str:
    url_suffix = Path(urlparse(url).path).suffix.lstrip(".").lower()
    if url_suffix:
        return url_suffix

    source = file_name or ""
    file_suffix = Path(source).suffix.lstrip(".").lower()
    if file_suffix:
        return file_suffix

    defaults = {"image": "jpg", "voice": "silk", "video": "mp4", "file": "bin", "json": "json", "card_page": "html"}
    return defaults[media_type]


def _media_type_from_url(url: str, fallback: str = "file") -> str:
    suffix = Path(urlparse(url).path).suffix.lstrip(".").lower()
    if suffix in {"png", "jpg", "jpeg", "gif", "webp", "bmp"}:
        return "image"
    if suffix in {"mp3", "wav", "ogg", "silk", "amr", "m4a", "flac"}:
        return "voice"
    if suffix in {"mp4", "webm", "mov", "mkv", "avi"}:
        return "video"
    return fallback


def _normalize_http_url(url: str) -> str | None:
    value = url.strip()
    if value.startswith(("http://", "https://")):
        return value
    if value.startswith("//"):
        return f"https:{value}"
    if "." in value and not value.startswith(("/", "#")):
        return f"https://{value}"
    return None


def _looks_like_media_url(url: str, key: str | None = None) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    lowered_key = (key or "").lower()
    if lowered_key in _CARD_MEDIA_KEYS:
        return True
    if any(part in parsed.netloc.lower() for part in _KNOWN_MEDIA_HOST_PARTS):
        return True
    suffix = Path(parsed.path).suffix.lstrip(".").lower()
    return suffix in _MEDIA_EXTENSIONS


def _looks_like_card_page_url(url: str, key: str | None = None) -> bool:
    lowered_key = (key or "").lower()
    if lowered_key not in _CARD_PAGE_URL_KEYS:
        return False
    normalized = _normalize_http_url(url)
    if normalized is None:
        return False
    return not _looks_like_media_url(normalized, key)


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
    async def download_url_to_local_path(
        db: AsyncSession,
        url: str,
        media_type: str = "file",
        file_name: str | None = None,
        http_client: Any | None = None,
        storage_root: str | Path | None = None,
        public_prefix: str | None = None,
        max_bytes: int | None = None,
    ) -> str | None:
        from app.services.message_service import MessageService

        media_max_bytes = max_bytes if max_bytes is not None else get_settings().media_max_bytes
        owns_client = http_client is None
        client = http_client or httpx.AsyncClient()
        try:
            try:
                response = await client.get(url)
            except (httpx.HTTPError, KeyError):
                return None
            if response.status_code >= 400:
                return None
            content_length = response.headers.get("content-length")
            if content_length is not None and content_length.isdigit() and int(content_length) > media_max_bytes:
                return None
            if len(response.content) > media_max_bytes:
                return None
            return await MessageService.save_media_asset(
                db,
                file_content=response.content,
                file_type=media_type,
                ext=_guess_ext(url, file_name, media_type),
                storage_root=storage_root,
                public_prefix=public_prefix,
            )
        finally:
            if owns_client:
                await client.aclose()

    @staticmethod
    async def _localize_json_value(
        db: AsyncSession,
        value: Any,
        http_client: Any | None,
        storage_root: str | Path | None,
        public_prefix: str | None,
        max_bytes: int | None,
        key: str | None = None,
    ) -> Any:
        if isinstance(value, dict):
            localized: dict[str, Any] = {}
            for child_key, child_value in value.items():
                localized[child_key] = await MediaService._localize_json_value(
                    db,
                    child_value,
                    http_client,
                    storage_root,
                    public_prefix,
                    max_bytes,
                    str(child_key),
                )
            return localized
        if isinstance(value, list):
            return [
                await MediaService._localize_json_value(
                    db,
                    item,
                    http_client,
                    storage_root,
                    public_prefix,
                    max_bytes,
                    key,
                )
                for item in value
            ]
        if isinstance(value, str) and _looks_like_media_url(value, key):
            local_path = await MediaService.download_url_to_local_path(
                db,
                value,
                media_type=_media_type_from_url(value),
                http_client=http_client,
                storage_root=storage_root,
                public_prefix=public_prefix,
                max_bytes=max_bytes,
            )
            return local_path or value
        return value

    @staticmethod
    async def _cache_card_page_url(
        db: AsyncSession,
        url: str,
        http_client: Any | None,
        storage_root: str | Path | None,
        public_prefix: str | None,
        max_bytes: int | None,
    ) -> str | None:
        normalized = _normalize_http_url(url)
        if normalized is None:
            return None
        return await MediaService.download_url_to_local_path(
            db,
            normalized,
            media_type="card_page",
            file_name="card.html",
            http_client=http_client,
            storage_root=storage_root,
            public_prefix=public_prefix,
            max_bytes=max_bytes,
        )

    @staticmethod
    async def _cache_card_page_snapshots(
        db: AsyncSession,
        value: Any,
        http_client: Any | None,
        storage_root: str | Path | None,
        public_prefix: str | None,
        max_bytes: int | None,
        key: str | None = None,
    ) -> Any:
        if isinstance(value, dict):
            cached: dict[str, Any] = {}
            page_candidates: list[str] = []
            existing_local_page = value.get("local_page")
            for child_key, child_value in value.items():
                cached[child_key] = await MediaService._cache_card_page_snapshots(
                    db,
                    child_value,
                    http_client,
                    storage_root,
                    public_prefix,
                    max_bytes,
                    str(child_key),
                )
                if isinstance(child_value, str) and _looks_like_card_page_url(child_value, str(child_key)):
                    page_candidates.append(child_value)
            if not existing_local_page:
                for candidate in page_candidates:
                    local_page = await MediaService._cache_card_page_url(
                        db,
                        candidate,
                        http_client,
                        storage_root,
                        public_prefix,
                        max_bytes,
                    )
                    if local_page:
                        cached["local_page"] = local_page
                        break
            return cached
        if isinstance(value, list):
            return [
                await MediaService._cache_card_page_snapshots(
                    db,
                    item,
                    http_client,
                    storage_root,
                    public_prefix,
                    max_bytes,
                    key,
                )
                for item in value
            ]
        return value

    @staticmethod
    async def _localize_card_data(
        db: AsyncSession,
        data: str,
        http_client: Any | None,
        storage_root: str | Path | None,
        public_prefix: str | None,
        max_bytes: int | None,
    ) -> str:
        decoded = html.unescape(data)
        try:
            payload = json.loads(decoded)
        except json.JSONDecodeError:
            rebuilt = []
            cursor = 0
            for match in _URL_PATTERN.finditer(decoded):
                rebuilt.append(decoded[cursor:match.start()])
                url = match.group(0)
                local_path = None
                if _looks_like_media_url(url):
                    local_path = await MediaService.download_url_to_local_path(
                        db,
                        url,
                        media_type=_media_type_from_url(url),
                        http_client=http_client,
                        storage_root=storage_root,
                        public_prefix=public_prefix,
                        max_bytes=max_bytes,
                    )
                rebuilt.append(local_path or url)
                cursor = match.end()
            rebuilt.append(decoded[cursor:])
            return "".join(rebuilt)

        localized = await MediaService._localize_json_value(
            db,
            payload,
            http_client,
            storage_root,
            public_prefix,
            max_bytes,
        )
        localized = await MediaService._cache_card_page_snapshots(
            db,
            localized,
            http_client,
            storage_root,
            public_prefix,
            max_bytes,
        )
        return json.dumps(localized, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    async def rewrite_cq_media_to_local_paths(
        db: AsyncSession,
        raw_message: str,
        http_client: Any | None = None,
        storage_root: str | Path | None = None,
        public_prefix: str | None = None,
        max_bytes: int | None = None,
    ) -> str:
        rewritten = raw_message

        for segment in parse_cq_media_segments(raw_message):
            local_path = await MediaService.download_url_to_local_path(
                db,
                segment.url,
                media_type=segment.media_type,
                file_name=f"asset.{segment.ext}",
                http_client=http_client,
                storage_root=storage_root,
                public_prefix=public_prefix,
                max_bytes=max_bytes,
            )
            if local_path:
                rewritten = rewritten.replace(segment.raw, f"\n{local_path}\n", 1)

        for match in list(_CQ_PATTERN.finditer(rewritten)):
            cq_type = match.group("kind")
            if cq_type not in {"json", "xml"}:
                continue
            params = _parse_cq_params(match.group("params"))
            data = params.get("data")
            if not data:
                continue
            params["data"] = await MediaService._localize_card_data(
                db,
                data,
                http_client=http_client,
                storage_root=storage_root,
                public_prefix=public_prefix,
                max_bytes=max_bytes,
            )
            rewritten = rewritten.replace(match.group(0), _build_cq_segment(cq_type, params), 1)

        return rewritten.strip()

    @staticmethod
    async def cache_cq_forward_payloads(
        db: AsyncSession,
        local_message: str,
        forward_loader: Any,
        http_client: Any | None = None,
        storage_root: str | Path | None = None,
        public_prefix: str | None = None,
        max_bytes: int | None = None,
    ) -> str:
        from app.services.message_service import MessageService

        rewritten = local_message
        for match in list(_CQ_PATTERN.finditer(local_message)):
            if match.group("kind") != "forward":
                continue
            params = _parse_cq_params(match.group("params"))
            forward_id = params.get("id")
            if not forward_id or params.get("local"):
                continue
            try:
                payload = await forward_loader(forward_id)
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            localized = await MediaService.localize_onebot_payload(
                db,
                payload,
                http_client=http_client,
                storage_root=storage_root,
                public_prefix=public_prefix,
                max_bytes=max_bytes,
            )
            local_path = await MessageService.save_media_asset(
                db,
                file_content=json.dumps(localized, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                file_type="forward",
                ext="json",
                storage_root=storage_root,
                public_prefix=public_prefix,
            )
            params["local"] = local_path
            rewritten = rewritten.replace(match.group(0), _build_cq_segment("forward", params), 1)
        return rewritten

    @staticmethod
    async def localize_onebot_content(
        db: AsyncSession,
        content: Any,
        http_client: Any | None = None,
        storage_root: str | Path | None = None,
        public_prefix: str | None = None,
        max_bytes: int | None = None,
    ) -> Any:
        if isinstance(content, str):
            return await MediaService.rewrite_cq_media_to_local_paths(
                db,
                content,
                http_client=http_client,
                storage_root=storage_root,
                public_prefix=public_prefix,
                max_bytes=max_bytes,
            )
        if isinstance(content, list):
            return [
                await MediaService.localize_onebot_content(
                    db,
                    item,
                    http_client=http_client,
                    storage_root=storage_root,
                    public_prefix=public_prefix,
                    max_bytes=max_bytes,
                )
                for item in content
            ]
        if not isinstance(content, dict):
            return content

        segment = deepcopy(content)
        segment_type = segment.get("type") or segment.get("kind")
        data = segment.get("data") if isinstance(segment.get("data"), dict) else segment
        if isinstance(data, dict):
            if segment_type in {"image", "record", "video", "file"}:
                url = data.get("url") or data.get("path")
                if isinstance(url, str) and url.startswith(("http://", "https://")):
                    media_type = {"record": "voice"}.get(str(segment_type), str(segment_type))
                    local_path = await MediaService.download_url_to_local_path(
                        db,
                        url,
                        media_type=media_type,
                        file_name=data.get("file"),
                        http_client=http_client,
                        storage_root=storage_root,
                        public_prefix=public_prefix,
                        max_bytes=max_bytes,
                    )
                    if local_path:
                        data["url"] = local_path
            elif segment_type in {"json", "xml"} and isinstance(data.get("data"), str):
                data["data"] = await MediaService._localize_card_data(
                    db,
                    data["data"],
                    http_client=http_client,
                    storage_root=storage_root,
                    public_prefix=public_prefix,
                    max_bytes=max_bytes,
                )
            else:
                localized = await MediaService._localize_json_value(
                    db,
                    data,
                    http_client,
                    storage_root,
                    public_prefix,
                    max_bytes,
                )
                if segment.get("data") is data:
                    segment["data"] = localized
                else:
                    segment = localized
        return segment

    @staticmethod
    async def localize_onebot_payload(
        db: AsyncSession,
        payload: dict[str, Any],
        http_client: Any | None = None,
        storage_root: str | Path | None = None,
        public_prefix: str | None = None,
        max_bytes: int | None = None,
    ) -> dict[str, Any]:
        localized = deepcopy(payload)
        data = localized.get("data", localized)
        messages = data.get("messages") if isinstance(data, dict) else None
        if not isinstance(messages, list):
            return localized
        for item in messages:
            if not isinstance(item, dict):
                continue
            if isinstance(item.get("raw_message"), str):
                item["raw_message"] = await MediaService.rewrite_cq_media_to_local_paths(
                    db,
                    item["raw_message"],
                    http_client=http_client,
                    storage_root=storage_root,
                    public_prefix=public_prefix,
                    max_bytes=max_bytes,
                )
            if "message" in item:
                item["message"] = await MediaService.localize_onebot_content(
                    db,
                    item["message"],
                    http_client=http_client,
                    storage_root=storage_root,
                    public_prefix=public_prefix,
                    max_bytes=max_bytes,
                )
        return localized
