import asyncio
import html
import ipaddress
import json
import logging
import os
import re
import socket
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse, urlunparse

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.metrics import metrics_registry

logger = logging.getLogger(__name__)
_HTTPX_ASYNC_CLIENT_TYPE = httpx.AsyncClient


@dataclass(frozen=True)
class CQMediaSegment:
    raw: str
    media_type: str
    url: str
    ext: str


@dataclass(frozen=True)
class TranscodedMedia:
    content: bytes
    ext: str


_CQ_PATTERN = re.compile(r"\[CQ:(?P<kind>\w+),(?P<params>[^\]]+)\]")
_SUPPORTED_MEDIA_TYPES = {
    "image": "image",
    "record": "voice",
    "video": "video",
    "file": "file",
    "mface": "image",
    "market_face": "image",
    "bface": "image",
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
    "contenturl",
    "docurl",
    "jumpurl",
    "link",
    "pageurl",
    "qqdocurl",
    "shareurl",
    "targeturl",
    "url",
    "webpageurl",
    "weburl",
}
_CARD_PAGE_URL_KEY_PRIORITY = {
    "qqdocurl": 0,
    "docurl": 0,
    "weburl": 1,
    "webpageurl": 1,
    "targeturl": 2,
    "jumpurl": 2,
    "shareurl": 3,
    "pageurl": 3,
    "contenturl": 3,
    "link": 4,
    "url": 5,
}
_QQ_MINIAPP_HOSTS = {
    "m.q.qq.com",
    "q.qq.com",
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
    "svg",
}
_ALLOWED_DOWNLOAD_CONTENT_TYPES = {
    "image": {
        "image/bmp",
        "image/gif",
        "image/jpeg",
        "image/png",
        "image/webp",
    },
    "voice": {
        "application/octet-stream",
        "audio/aac",
        "audio/amr",
        "audio/m4a",
        "audio/mp4",
        "audio/mpeg",
        "audio/ogg",
        "audio/silk",
        "audio/wav",
        "audio/webm",
        "audio/x-m4a",
        "audio/x-wav",
    },
    "video": {
        "video/mp4",
        "video/quicktime",
        "video/webm",
        "video/x-matroska",
        "video/x-msvideo",
    },
    "file": {
        "application/gzip",
        "application/java-archive",
        "application/msword",
        "application/octet-stream",
        "application/pdf",
        "application/vnd.android.package-archive",
        "application/vnd.ms-excel",
        "application/vnd.ms-powerpoint",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/x-7z-compressed",
        "application/x-msdownload",
        "application/x-rar-compressed",
        "application/x-tar",
        "application/zip",
        "text/csv",
        "text/plain",
    },
    "card_page": {
        "application/xhtml+xml",
        "text/html",
        "text/plain",
    },
    "json": {
        "application/json",
        "text/json",
    },
}


def _normalize_content_type(content_type: str | None) -> str:
    return (content_type or "").split(";", 1)[0].strip().lower()


def _is_trusted_qq_media_url(url: str) -> bool:
    hostname = (urlparse(url).hostname or "").lower()
    return any(hostname == part or hostname.endswith(f".{part}") for part in _KNOWN_MEDIA_HOST_PARTS)


def _looks_like_supported_video_payload(content: bytes) -> bool:
    return len(content) >= 12 and (content[4:8] == b"ftyp" or content.startswith(b"\x1a\x45\xdf\xa3"))


def _is_allowed_download_content_type(media_type: str, content_type: str | None) -> bool:
    normalized = _normalize_content_type(content_type)
    if not normalized:
        return True
    allowed = _ALLOWED_DOWNLOAD_CONTENT_TYPES.get(media_type, _ALLOWED_DOWNLOAD_CONTENT_TYPES["file"])
    return normalized in allowed


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
    if value.startswith("//"):
        value = f"https:{value}"
    elif not value.startswith(("http://", "https://")):
        if "." not in value or value.startswith(("/", "#")):
            return None
        value = f"https://{value}"
    try:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return None
        port = parsed.port
        if port is not None and port not in {80, 443}:
            return None
    except ValueError:
        return None
    return value


def _is_public_ip_address(value: str) -> bool:
    address = ipaddress.ip_address(value)
    return not any((
        address.is_private,
        address.is_loopback,
        address.is_link_local,
        address.is_multicast,
        address.is_reserved,
        address.is_unspecified,
    ))


@dataclass(frozen=True)
class _DownloadTarget:
    """Where the request actually goes, and how it stays addressed correctly.

    Once a hostname has been resolved and every answer checked, ``url`` points
    at the literal address that was checked. Validating a name and then letting
    the HTTP client resolve it a second time leaves a window in which DNS can
    answer differently -- classic rebinding -- and the request lands on the
    private address the check was supposed to prevent.
    """

    url: str
    headers: dict[str, str]
    extensions: dict[str, Any]

    @property
    def is_pinned(self) -> bool:
        return bool(self.headers or self.extensions)


def _pin_download_target(parsed: Any, address: str) -> _DownloadTarget:
    literal = f"[{address}]" if ":" in address else address
    netloc = f"{literal}:{parsed.port}" if parsed.port else literal
    pinned_url = urlunparse(parsed._replace(netloc=netloc))
    # The origin still has to see its own name, and for TLS the certificate has
    # to be verified against that name rather than against the address.
    headers = {"Host": parsed.netloc}
    extensions: dict[str, Any] = {}
    if parsed.scheme == "https":
        extensions["sni_hostname"] = parsed.hostname
    return _DownloadTarget(pinned_url, headers, extensions)


async def _resolve_download_target(url: str, *, resolve_host: bool) -> _DownloadTarget | None:
    normalized = _normalize_http_url(url)
    if normalized is None:
        return None
    parsed = urlparse(normalized)
    host = parsed.hostname
    if not host:
        return None
    try:
        literal_is_public = _is_public_ip_address(host)
    except ValueError:
        literal_is_public = None
    if literal_is_public is False:
        return None
    if literal_is_public is True or not resolve_host:
        # Already an address, or the caller injected a transport that does its
        # own routing; there is nothing to pin.
        return _DownloadTarget(normalized, {}, {})
    try:
        loop = asyncio.get_running_loop()
        resolved = await loop.run_in_executor(None, lambda: socket.getaddrinfo(host, None, type=socket.SOCK_STREAM))
        addresses = [item[4][0] for item in resolved]
        if not addresses or any(not _is_public_ip_address(address) for address in addresses):
            return None
    except (OSError, ValueError):
        return None
    return _pin_download_target(parsed, addresses[0])


async def _send_download_request(client: Any, target: _DownloadTarget) -> Any:
    if isinstance(client, _HTTPX_ASYNC_CLIENT_TYPE):
        request = client.build_request(
            "GET",
            target.url,
            headers=target.headers or None,
            extensions=target.extensions or None,
        )
        return await client.send(request, follow_redirects=False, stream=True)
    if target.is_pinned:
        return await client.get(
            target.url,
            headers=target.headers,
            extensions=target.extensions,
            follow_redirects=False,
        )
    # Injected stubs accept only the URL.
    try:
        return await client.get(target.url, follow_redirects=False)
    except TypeError:
        return await client.get(target.url)


async def _read_response_bytes(response: Any, max_bytes: int) -> bytes | None:
    content = bytearray()
    iterator = getattr(response, "aiter_bytes", None)
    if callable(iterator):
        async for chunk in iterator():
            content.extend(chunk)
            if len(content) > max_bytes:
                return None
        return bytes(content)
    body = bytes(response.content)
    return body if len(body) <= max_bytes else None


def _normalize_card_url_key(key: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (key or "").lower())


def _is_qq_miniapp_shell_url(url: str) -> bool:
    normalized = _normalize_http_url(url)
    if normalized is None:
        return False
    parsed = urlparse(normalized)
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    return host in _QQ_MINIAPP_HOSTS and (path.startswith("/a/s/") or "miniapp" in path)


def _card_page_url_priority(url: str, key: str | None = None) -> tuple[int, int, int, str]:
    normalized_key = _normalize_card_url_key(key)
    key_score = _CARD_PAGE_URL_KEY_PRIORITY.get(normalized_key, 9)
    shell_score = 1 if _is_qq_miniapp_shell_url(url) else 0
    return (shell_score, key_score, len(url), url)


def _sort_card_page_candidates(candidates: list[tuple[str, str | None]]) -> list[str]:
    unique: dict[str, str | None] = {}
    for url, key in candidates:
        normalized = _normalize_http_url(url)
        if normalized is None:
            continue
        unique.setdefault(normalized, key)
    return [
        url
        for url, key in sorted(unique.items(), key=lambda item: _card_page_url_priority(item[0], item[1]))
    ]


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
    lowered_key = _normalize_card_url_key(key)
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


def _parse_cq_media_segments_with_ordinals(
    raw_message: str,
    public_prefix: str | None = None,
) -> list[tuple[int, CQMediaSegment]]:
    """Parse remote CQ media using the same ordinal slots as local message media."""

    prefix = (public_prefix or get_settings().public_storage_prefix).rstrip("/") + "/"
    local_path_pattern = rf"{re.escape(prefix)}[^\s\"'<>),\]]+?\.[a-z0-9]+"
    ordinal_pattern = re.compile(
        rf"\[CQ:(?P<kind>image|record|video|file|mface|market_face|bface),(?P<params>[^\]]*)\]|(?P<local>{local_path_pattern})",
        re.I,
    )
    segments: list[tuple[int, CQMediaSegment]] = []
    ordinal = 0
    for match in ordinal_pattern.finditer(raw_message):
        if match.group("local") is None:
            cq_type = (match.group("kind") or "").lower()
            media_type = _SUPPORTED_MEDIA_TYPES[cq_type]
            params = _parse_cq_params(match.group("params") or "")
            url = params.get("url")
            if url:
                file_name = params.get("file")
                segments.append(
                    (
                        ordinal,
                        CQMediaSegment(
                            raw=match.group(0),
                            media_type=media_type,
                            url=url,
                            ext=_guess_ext(url, file_name, media_type),
                        ),
                    )
                )
        ordinal += 1
    return segments


class MediaService:
    @staticmethod
    def _target_transcode_ext(media_type: str, voice_ext: str, video_ext: str) -> str | None:
        if media_type == "voice":
            return voice_ext.lstrip(".").lower() or "mp3"
        if media_type == "video":
            return video_ext.lstrip(".").lower() or "mp4"
        return None

    @staticmethod
    def _ffmpeg_args_for(media_type: str, target_ext: str) -> list[str]:
        if media_type == "voice":
            if target_ext == "ogg":
                return ["-vn", "-f", "ogg", "-codec:a", "libopus", "pipe:1"]
            if target_ext == "wav":
                return ["-vn", "-f", "wav", "pipe:1"]
            return ["-vn", "-f", "mp3", "-codec:a", "libmp3lame", "pipe:1"]
        if media_type == "video":
            return [
                "-f",
                "mp4",
                "-movflags",
                "frag_keyframe+empty_moov",
                "-codec:v",
                "libx264",
                "-preset",
                "veryfast",
                "-codec:a",
                "aac",
                "pipe:1",
            ]
        return []

    @staticmethod
    async def transcode_media_bytes(
        content: bytes,
        media_type: str,
        *,
        ffmpeg_bin: str,
        ffmpeg_library_path: str = "",
        timeout_seconds: int = 60,
        voice_ext: str = "mp3",
        video_ext: str = "mp4",
    ) -> TranscodedMedia | None:
        target_ext = MediaService._target_transcode_ext(media_type, voice_ext, video_ext)
        if target_ext is None:
            return None

        command = [
            ffmpeg_bin,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            "pipe:0",
            *MediaService._ffmpeg_args_for(media_type, target_ext),
        ]
        env = None
        library_path = ffmpeg_library_path.strip()
        if library_path:
            env = os.environ.copy()
            env["LD_LIBRARY_PATH"] = library_path
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            try:
                stdout, _stderr = await asyncio.wait_for(process.communicate(content), timeout=timeout_seconds)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                logger.warning(
                    "FFmpeg transcode timeout",
                    extra={"media_type": media_type, "target_ext": target_ext, "timeout_seconds": timeout_seconds},
                )
                return None
        except (FileNotFoundError, OSError) as exc:
            logger.warning(
                "FFmpeg transcode unavailable",
                extra={"media_type": media_type, "target_ext": target_ext, "ffmpeg_bin": ffmpeg_bin, "error": str(exc)},
            )
            return None

        if process.returncode != 0 or not stdout:
            logger.warning(
                "FFmpeg transcode failed",
                extra={"media_type": media_type, "target_ext": target_ext, "returncode": process.returncode},
            )
            return None
        return TranscodedMedia(content=stdout, ext=target_ext)

    @staticmethod
    async def _maybe_transcode_media(
        content: bytes,
        media_type: str,
        ext: str,
        *,
        enabled: bool,
        ffmpeg_bin: str,
        ffmpeg_library_path: str,
        voice_ext: str,
        video_ext: str,
        transcode_timeout_seconds: int,
        max_bytes: int,
    ) -> tuple[bytes, str]:
        if not enabled:
            return content, ext

        transcoded = await MediaService.transcode_media_bytes(
            content,
            media_type,
            ffmpeg_bin=ffmpeg_bin,
            ffmpeg_library_path=ffmpeg_library_path,
            timeout_seconds=transcode_timeout_seconds,
            voice_ext=voice_ext,
            video_ext=video_ext,
        )
        if transcoded is None or len(transcoded.content) > max_bytes:
            return content, ext
        return transcoded.content, transcoded.ext

    @staticmethod
    async def save_unavailable_placeholder(
        db: AsyncSession,
        url: str,
        media_type: str = "file",
        reason: str = "unavailable",
        storage_root: str | Path | None = None,
        public_prefix: str | None = None,
    ) -> str:
        from app.services.message_service import MessageService

        escaped_url = html.escape(url, quote=True)
        escaped_reason = html.escape(reason, quote=True)
        if media_type == "image":
            content = (
                '<svg xmlns="http://www.w3.org/2000/svg" width="420" height="120" viewBox="0 0 420 120">'
                '<rect width="420" height="120" fill="#e2e8f0"/>'
                '<text x="24" y="48" fill="#334155" font-size="18" font-family="Arial, sans-serif">media unavailable</text>'
                f'<text x="24" y="78" fill="#64748b" font-size="12" font-family="Arial, sans-serif">{escaped_reason}</text>'
                f"<desc>{escaped_url}</desc>"
                "</svg>"
            ).encode("utf-8")
            return await MessageService.save_media_asset(
                db,
                file_content=content,
                file_type="image_missing",
                ext="svg",
                storage_root=storage_root,
                public_prefix=public_prefix,
            )
        if media_type == "card_page":
            content = (
                "<!doctype html><meta charset=\"utf-8\"><title>卡片网页未缓存</title>"
                "<body style=\"font-family:sans-serif;line-height:1.6;padding:24px\">"
                "<h1>卡片网页未缓存</h1>"
                f"<p>原因：{escaped_reason}</p>"
                f"<p>原始地址：<code>{escaped_url}</code></p>"
                "</body>"
            ).encode("utf-8")
            return await MessageService.save_media_asset(
                db,
                file_content=content,
                file_type="card_missing",
                ext="html",
                storage_root=storage_root,
                public_prefix=public_prefix,
            )

        content = f"media unavailable\nreason: {reason}\nurl: {url}\n".encode("utf-8")
        file_type = f"{media_type}_missing"[:20]
        return await MessageService.save_media_asset(
            db,
            file_content=content,
            file_type=file_type,
            ext="txt",
            storage_root=storage_root,
            public_prefix=public_prefix,
        )

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
        transcode_enabled: bool | None = None,
        ffmpeg_bin: str | None = None,
    ) -> str | None:
        from app.services.message_service import MessageService

        settings = get_settings()
        media_max_bytes = max_bytes if max_bytes is not None else settings.media_max_bytes
        owns_client = http_client is None
        client = http_client or httpx.AsyncClient()
        response: Any | None = None
        download_status = "unknown"
        try:
            target = await _resolve_download_target(url, resolve_host=owns_client or type(client).__module__.startswith("httpx"))
            if target is None:
                download_status = "unsafe_url"
                logger.warning("Media download rejected unsafe URL", extra={"url": url, "media_type": media_type})
                return None
            # Keep logging and downstream checks keyed on the name, not on the
            # pinned address, so messages stay readable and host allowlists
            # (trusted QQ media domains) still match.
            normalized_url = _normalize_http_url(url) or url
            try:
                response = await _send_download_request(client, target)
            except (httpx.HTTPError, KeyError) as exc:
                download_status = "request_error"
                logger.warning(
                    "Media download request failed",
                    extra={"url": normalized_url, "media_type": media_type, "error": str(exc)},
                )
                return None
            if 300 <= response.status_code < 400:
                download_status = "redirect_rejected"
                logger.warning(
                    "Media download redirect rejected",
                    extra={"url": normalized_url, "media_type": media_type, "location": response.headers.get("location")},
                )
                return None
            if response.status_code >= 400:
                download_status = "upstream_error"
                logger.warning(
                    "Media download rejected by upstream",
                    extra={"url": normalized_url, "media_type": media_type, "status_code": response.status_code},
                )
                return None
            content_type = _normalize_content_type(response.headers.get("content-type"))
            is_trusted_generic_video = (
                media_type == "video"
                and content_type == "application/octet-stream"
                and _is_trusted_qq_media_url(normalized_url)
            )
            if not _is_allowed_download_content_type(media_type, content_type) and not is_trusted_generic_video:
                download_status = "content_type_rejected"
                logger.warning(
                    "Media download content type rejected",
                    extra={"url": normalized_url, "media_type": media_type, "content_type": content_type},
                )
                return None
            content_length = response.headers.get("content-length")
            if content_length is not None and content_length.isdigit() and int(content_length) > media_max_bytes:
                download_status = "too_large"
                logger.warning(
                    "Media download content length exceeds limit",
                    extra={"url": normalized_url, "media_type": media_type, "content_length": int(content_length), "max_bytes": media_max_bytes},
                )
                return None
            content = await _read_response_bytes(response, media_max_bytes)
            if content is None:
                download_status = "too_large"
                logger.warning(
                    "Media download body exceeds limit",
                    extra={"url": normalized_url, "media_type": media_type, "max_bytes": media_max_bytes},
                )
                return None
            if is_trusted_generic_video and not _looks_like_supported_video_payload(content):
                download_status = "unsupported_video_payload"
                logger.warning(
                    "Media download rejected unsupported QQ video payload",
                    extra={"url": normalized_url, "content_type": content_type},
                )
                return None
            ext = _guess_ext(normalized_url, file_name, media_type)
            content, ext = await MediaService._maybe_transcode_media(
                content,
                media_type,
                ext,
                enabled=settings.media_transcode_enabled if transcode_enabled is None else transcode_enabled,
                ffmpeg_bin=ffmpeg_bin or settings.ffmpeg_bin,
                ffmpeg_library_path=settings.ffmpeg_library_path,
                voice_ext=settings.media_transcode_voice_ext,
                video_ext=settings.media_transcode_video_ext,
                transcode_timeout_seconds=settings.media_transcode_timeout_seconds,
                max_bytes=media_max_bytes,
            )
            local_path = await MessageService.save_media_asset(
                db,
                file_content=content,
                file_type=media_type,
                ext=ext,
                storage_root=storage_root,
                public_prefix=public_prefix,
            )
            download_status = "success"
            return local_path
        finally:
            metrics_registry.record_media_download(media_type=media_type, status=download_status)
            if response is not None:
                close_response = getattr(response, "aclose", None)
                if callable(close_response):
                    await close_response()
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
        unavailable_placeholders: bool = False,
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
                    unavailable_placeholders,
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
                    unavailable_placeholders,
                    key,
                )
                for item in value
            ]
        if isinstance(value, str) and _looks_like_media_url(value, key):
            media_type = _media_type_from_url(value)
            local_path = await MediaService.download_url_to_local_path(
                db,
                value,
                media_type=media_type,
                http_client=http_client,
                storage_root=storage_root,
                public_prefix=public_prefix,
                max_bytes=max_bytes,
            )
            if local_path is None and unavailable_placeholders:
                local_path = await MediaService.save_unavailable_placeholder(
                    db,
                    value,
                    media_type=media_type,
                    reason="download_failed_or_expired",
                    storage_root=storage_root,
                    public_prefix=public_prefix,
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
        unavailable_placeholders: bool = False,
    ) -> str | None:
        normalized = _normalize_http_url(url)
        if normalized is None:
            return None
        local_path = await MediaService.download_url_to_local_path(
            db,
            normalized,
            media_type="card_page",
            file_name="card.html",
            http_client=http_client,
            storage_root=storage_root,
            public_prefix=public_prefix,
            max_bytes=max_bytes,
        )
        if local_path is None and unavailable_placeholders:
            local_path = await MediaService.save_unavailable_placeholder(
                db,
                normalized,
                media_type="card_page",
                reason="snapshot_failed_or_unavailable",
                storage_root=storage_root,
                public_prefix=public_prefix,
            )
        return local_path

    @staticmethod
    async def _cache_card_page_snapshots(
        db: AsyncSession,
        value: Any,
        http_client: Any | None,
        storage_root: str | Path | None,
        public_prefix: str | None,
        max_bytes: int | None,
        unavailable_placeholders: bool = False,
        key: str | None = None,
    ) -> Any:
        if isinstance(value, dict):
            cached: dict[str, Any] = {}
            page_candidates: list[tuple[str, str | None]] = []
            existing_local_page = value.get("local_page")
            for child_key, child_value in value.items():
                cached[child_key] = await MediaService._cache_card_page_snapshots(
                    db,
                    child_value,
                    http_client,
                    storage_root,
                    public_prefix,
                    max_bytes,
                    unavailable_placeholders,
                    str(child_key),
                )
                if isinstance(child_value, str) and _looks_like_card_page_url(child_value, str(child_key)):
                    page_candidates.append((child_value, str(child_key)))
            if not existing_local_page:
                for candidate in _sort_card_page_candidates(page_candidates):
                    local_page = await MediaService._cache_card_page_url(
                        db,
                        candidate,
                        http_client,
                        storage_root,
                        public_prefix,
                        max_bytes,
                        unavailable_placeholders,
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
                    unavailable_placeholders,
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
        unavailable_placeholders: bool = False,
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
                    media_type = _media_type_from_url(url)
                    local_path = await MediaService.download_url_to_local_path(
                        db,
                        url,
                        media_type=media_type,
                        http_client=http_client,
                        storage_root=storage_root,
                        public_prefix=public_prefix,
                        max_bytes=max_bytes,
                    )
                    if local_path is None and unavailable_placeholders:
                        local_path = await MediaService.save_unavailable_placeholder(
                            db,
                            url,
                            media_type=media_type,
                            reason="download_failed_or_expired",
                            storage_root=storage_root,
                            public_prefix=public_prefix,
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
            unavailable_placeholders,
        )
        localized = await MediaService._cache_card_page_snapshots(
            db,
            localized,
            http_client,
            storage_root,
            public_prefix,
            max_bytes,
            unavailable_placeholders,
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
        unavailable_placeholders: bool = False,
        allowed_media_types: set[str] | None = None,
        skip_media_ordinals: set[int] | None = None,
    ) -> str:
        rewritten = raw_message

        if skip_media_ordinals is None:
            ordinal_segments = list(enumerate(parse_cq_media_segments(raw_message)))
        else:
            ordinal_segments = _parse_cq_media_segments_with_ordinals(raw_message, public_prefix)
        search_start = 0
        for ordinal, segment in ordinal_segments:
            segment_start = rewritten.find(segment.raw, search_start)
            if segment_start < 0:
                continue
            segment_end = segment_start + len(segment.raw)
            if skip_media_ordinals is not None and ordinal in skip_media_ordinals:
                search_start = segment_end
                continue
            if allowed_media_types is not None and segment.media_type not in allowed_media_types:
                search_start = segment_end
                continue
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
            if local_path is None and unavailable_placeholders:
                local_path = await MediaService.save_unavailable_placeholder(
                    db,
                    segment.url,
                    media_type=segment.media_type,
                    reason="download_failed_or_expired",
                    storage_root=storage_root,
                    public_prefix=public_prefix,
                )
            if local_path:
                replacement = f"\n{local_path}\n"
                rewritten = rewritten[:segment_start] + replacement + rewritten[segment_end:]
                search_start = segment_start + len(replacement)
            else:
                search_start = segment_end

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
                unavailable_placeholders=unavailable_placeholders,
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
        unavailable_placeholders: bool = False,
        allowed_media_types: set[str] | None = None,
        forward_depth: int = 3,
        seen_forward_ids: set[str] | None = None,
    ) -> str:
        rewritten = local_message
        seen = seen_forward_ids or set()
        for match in list(_CQ_PATTERN.finditer(local_message)):
            if match.group("kind") != "forward":
                continue
            params = _parse_cq_params(match.group("params"))
            forward_id = params.get("id")
            if not forward_id or params.get("local"):
                continue
            local_path = await MediaService._cache_forward_payload_by_id(
                db,
                forward_id=forward_id,
                forward_loader=forward_loader,
                http_client=http_client,
                storage_root=storage_root,
                public_prefix=public_prefix,
                max_bytes=max_bytes,
                unavailable_placeholders=unavailable_placeholders,
                allowed_media_types=allowed_media_types,
                forward_depth=forward_depth,
                seen_forward_ids=seen,
            )
            if local_path:
                params["local"] = local_path
                rewritten = rewritten.replace(match.group(0), _build_cq_segment("forward", params), 1)
        return rewritten

    @staticmethod
    async def _cache_forward_payload_by_id(
        db: AsyncSession,
        forward_id: str,
        forward_loader: Any,
        http_client: Any | None = None,
        storage_root: str | Path | None = None,
        public_prefix: str | None = None,
        max_bytes: int | None = None,
        unavailable_placeholders: bool = False,
        allowed_media_types: set[str] | None = None,
        forward_depth: int = 3,
        seen_forward_ids: set[str] | None = None,
    ) -> str | None:
        from app.services.message_service import MessageService

        if forward_depth <= 0:
            return None
        seen = seen_forward_ids or set()
        if forward_id in seen:
            return None
        seen.add(forward_id)
        try:
            payload = await forward_loader(forward_id)
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        localized = await MediaService.localize_onebot_payload(
            db,
            payload,
            http_client=http_client,
            storage_root=storage_root,
            public_prefix=public_prefix,
            max_bytes=max_bytes,
            unavailable_placeholders=unavailable_placeholders,
            allowed_media_types=allowed_media_types,
        )
        localized = await MediaService.cache_nested_forward_payloads(
            db,
            localized,
            forward_loader=forward_loader,
            http_client=http_client,
            storage_root=storage_root,
            public_prefix=public_prefix,
            max_bytes=max_bytes,
            unavailable_placeholders=unavailable_placeholders,
            allowed_media_types=allowed_media_types,
            forward_depth=forward_depth - 1,
            seen_forward_ids=seen,
        )
        return await MessageService.save_media_asset(
            db,
            file_content=json.dumps(localized, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            file_type="forward",
            ext="json",
            storage_root=storage_root,
            public_prefix=public_prefix,
        )

    @staticmethod
    async def cache_nested_forward_payloads(
        db: AsyncSession,
        payload: dict[str, Any],
        forward_loader: Any,
        http_client: Any | None = None,
        storage_root: str | Path | None = None,
        public_prefix: str | None = None,
        max_bytes: int | None = None,
        unavailable_placeholders: bool = False,
        allowed_media_types: set[str] | None = None,
        forward_depth: int = 2,
        seen_forward_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        if forward_depth <= 0:
            return payload
        localized = deepcopy(payload)
        data = localized.get("data", localized)
        messages = data.get("messages") if isinstance(data, dict) else None
        if messages is None and isinstance(data, dict):
            messages = data.get("message")
        if not isinstance(messages, list):
            return localized
        for item in messages:
            if not isinstance(item, dict):
                continue
            if isinstance(item.get("raw_message"), str):
                item["raw_message"] = await MediaService.cache_cq_forward_payloads(
                    db,
                    local_message=item["raw_message"],
                    forward_loader=forward_loader,
                    http_client=http_client,
                    storage_root=storage_root,
                    public_prefix=public_prefix,
                    max_bytes=max_bytes,
                    unavailable_placeholders=unavailable_placeholders,
                    allowed_media_types=allowed_media_types,
                    forward_depth=forward_depth,
                    seen_forward_ids=seen_forward_ids,
                )
            for key in ("message", "content"):
                if key in item:
                    item[key] = await MediaService.cache_nested_forward_content(
                        db,
                        item[key],
                        forward_loader=forward_loader,
                        http_client=http_client,
                        storage_root=storage_root,
                        public_prefix=public_prefix,
                        max_bytes=max_bytes,
                        unavailable_placeholders=unavailable_placeholders,
                        allowed_media_types=allowed_media_types,
                        forward_depth=forward_depth,
                        seen_forward_ids=seen_forward_ids,
                    )
        return localized

    @staticmethod
    async def cache_nested_forward_content(
        db: AsyncSession,
        content: Any,
        forward_loader: Any,
        http_client: Any | None = None,
        storage_root: str | Path | None = None,
        public_prefix: str | None = None,
        max_bytes: int | None = None,
        unavailable_placeholders: bool = False,
        allowed_media_types: set[str] | None = None,
        forward_depth: int = 2,
        seen_forward_ids: set[str] | None = None,
    ) -> Any:
        if forward_depth <= 0:
            return content
        if isinstance(content, str):
            return await MediaService.cache_cq_forward_payloads(
                db,
                local_message=content,
                forward_loader=forward_loader,
                http_client=http_client,
                storage_root=storage_root,
                public_prefix=public_prefix,
                max_bytes=max_bytes,
                unavailable_placeholders=unavailable_placeholders,
                allowed_media_types=allowed_media_types,
                forward_depth=forward_depth,
                seen_forward_ids=seen_forward_ids,
            )
        if isinstance(content, list):
            return [
                await MediaService.cache_nested_forward_content(
                    db,
                    item,
                    forward_loader=forward_loader,
                    http_client=http_client,
                    storage_root=storage_root,
                    public_prefix=public_prefix,
                    max_bytes=max_bytes,
                    unavailable_placeholders=unavailable_placeholders,
                    allowed_media_types=allowed_media_types,
                    forward_depth=forward_depth,
                    seen_forward_ids=seen_forward_ids,
                )
                for item in content
            ]
        if not isinstance(content, dict):
            return content

        segment = deepcopy(content)
        segment_type = segment.get("type") or segment.get("kind")
        data = segment.get("data") if isinstance(segment.get("data"), dict) else segment
        if isinstance(data, dict) and segment_type == "forward":
            forward_id = data.get("id") or data.get("forward_id") or data.get("file")
            local_path = data.get("local") or data.get("local_path")
            if forward_id and not local_path:
                cached = await MediaService._cache_forward_payload_by_id(
                    db,
                    forward_id=str(forward_id),
                    forward_loader=forward_loader,
                    http_client=http_client,
                    storage_root=storage_root,
                    public_prefix=public_prefix,
                    max_bytes=max_bytes,
                    unavailable_placeholders=unavailable_placeholders,
                    allowed_media_types=allowed_media_types,
                    forward_depth=forward_depth,
                    seen_forward_ids=seen_forward_ids,
                )
                if cached:
                    data["local"] = cached
            return segment

        for key, value in list(segment.items()):
            segment[key] = await MediaService.cache_nested_forward_content(
                db,
                value,
                forward_loader=forward_loader,
                http_client=http_client,
                storage_root=storage_root,
                public_prefix=public_prefix,
                max_bytes=max_bytes,
                unavailable_placeholders=unavailable_placeholders,
                allowed_media_types=allowed_media_types,
                forward_depth=forward_depth,
                seen_forward_ids=seen_forward_ids,
            )
        return segment

    @staticmethod
    async def localize_onebot_content(
        db: AsyncSession,
        content: Any,
        http_client: Any | None = None,
        storage_root: str | Path | None = None,
        public_prefix: str | None = None,
        max_bytes: int | None = None,
        unavailable_placeholders: bool = False,
        allowed_media_types: set[str] | None = None,
    ) -> Any:
        if isinstance(content, str):
            return await MediaService.rewrite_cq_media_to_local_paths(
                db,
                content,
                http_client=http_client,
                storage_root=storage_root,
                public_prefix=public_prefix,
                max_bytes=max_bytes,
                unavailable_placeholders=unavailable_placeholders,
                allowed_media_types=allowed_media_types,
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
                    unavailable_placeholders=unavailable_placeholders,
                    allowed_media_types=allowed_media_types,
                )
                for item in content
            ]
        if not isinstance(content, dict):
            return content

        segment = deepcopy(content)
        segment_type = segment.get("type") or segment.get("kind")
        data = segment.get("data") if isinstance(segment.get("data"), dict) else segment
        if isinstance(data, dict):
            if segment_type in {"image", "record", "video", "file", "mface", "market_face", "bface"}:
                url = data.get("url") or data.get("path")
                media_type = {"record": "voice", "mface": "image", "market_face": "image", "bface": "image"}.get(str(segment_type), str(segment_type))
                if (
                    isinstance(url, str)
                    and url.startswith(("http://", "https://"))
                    and (allowed_media_types is None or media_type in allowed_media_types)
                ):
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
                    if local_path is None and unavailable_placeholders:
                        local_path = await MediaService.save_unavailable_placeholder(
                            db,
                            url,
                            media_type=media_type,
                            reason="download_failed_or_expired",
                            storage_root=storage_root,
                            public_prefix=public_prefix,
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
                    unavailable_placeholders=unavailable_placeholders,
                )
            else:
                localized = await MediaService._localize_json_value(
                    db,
                    data,
                    http_client,
                    storage_root,
                    public_prefix,
                    max_bytes,
                    unavailable_placeholders,
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
        unavailable_placeholders: bool = False,
        allowed_media_types: set[str] | None = None,
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
                    unavailable_placeholders=unavailable_placeholders,
                    allowed_media_types=allowed_media_types,
                )
            if "message" in item:
                item["message"] = await MediaService.localize_onebot_content(
                    db,
                    item["message"],
                    http_client=http_client,
                    storage_root=storage_root,
                    public_prefix=public_prefix,
                    max_bytes=max_bytes,
                    unavailable_placeholders=unavailable_placeholders,
                    allowed_media_types=allowed_media_types,
                )
        return localized
