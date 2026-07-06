import html
import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Message, RobotMessage
from app.services.media_service import (
    MediaService,
    _CQ_PATTERN,
    _URL_PATTERN,
    _build_cq_segment,
    _looks_like_media_url,
    _looks_like_card_page_url,
    _parse_cq_params,
    _sort_card_page_candidates,
    parse_cq_media_segments,
)


ForwardPayloadLoader = Callable[[str, str], Awaitable[dict[str, Any]]]

BACKFILL_FAILURE_DETAILS = {
    "download_failed_or_expired": (
        "媒体下载失败或源地址已过期",
        "确认机器人可访问源消息；如果无法恢复，可使用 finalize_unavailable 生成占位缓存。",
    ),
    "snapshot_failed_or_unavailable": (
        "卡片网页快照下载失败",
        "确认网页仍可访问；如果不可恢复，可使用 finalize_unavailable 生成缺失快照。",
    ),
    "forward_loader_unavailable": (
        "缺少合并转发拉取通道",
        "需要机器人在线并提供 get_forward_msg 能力后重新回填。",
    ),
    "forward_payload_unavailable": (
        "合并转发详情拉取失败",
        "确认对应机器人在线、消息仍可拉取；无法恢复时可生成缺失占位。",
    ),
}


@dataclass
class MediaBackfillFailure:
    msg_hash: str
    kind: str
    target: str
    reason: str
    label: str | None = None
    action: str | None = None

    def __post_init__(self) -> None:
        if self.label is not None and self.action is not None:
            return
        label, action = BACKFILL_FAILURE_DETAILS.get(self.reason, ("回填失败", "检查源消息、网络和抓取策略后重试。"))
        if self.label is None:
            self.label = label
        if self.action is None:
            self.action = action


@dataclass
class MediaBackfillReport:
    scanned: int = 0
    candidates: int = 0
    updated: int = 0
    unchanged: int = 0
    failed: int = 0
    media_failed: int = 0
    forward_failed: int = 0
    reason_summary: dict[str, int] = field(default_factory=dict)
    failures: list[MediaBackfillFailure] = field(default_factory=list)

    def add_failure(self, failure: MediaBackfillFailure, failure_limit: int) -> None:
        self.failed += 1
        self.reason_summary[failure.reason] = self.reason_summary.get(failure.reason, 0) + 1
        if failure.kind == "forward":
            self.forward_failed += 1
        else:
            self.media_failed += 1
        if len(self.failures) < failure_limit:
            self.failures.append(failure)


def _iter_card_media_urls(value: Any, key: str | None = None) -> list[str]:
    if isinstance(value, dict):
        urls: list[str] = []
        for child_key, child_value in value.items():
            urls.extend(_iter_card_media_urls(child_value, str(child_key)))
        return urls
    if isinstance(value, list):
        urls = []
        for item in value:
            urls.extend(_iter_card_media_urls(item, key))
        return urls
    if isinstance(value, str) and _looks_like_media_url(value, key):
        return [value]
    return []


def _find_card_media_urls(local_message: str) -> list[str]:
    urls: list[str] = []
    for match in _CQ_PATTERN.finditer(local_message):
        if match.group("kind") not in {"json", "xml"}:
            continue
        params = _parse_cq_params(match.group("params"))
        data = params.get("data")
        if not data:
            continue
        decoded = html.unescape(data)
        try:
            payload = json.loads(decoded)
        except json.JSONDecodeError:
            for url_match in _URL_PATTERN.finditer(decoded):
                url = url_match.group(0)
                if _looks_like_media_url(url):
                    urls.append(url)
        else:
            urls.extend(_iter_card_media_urls(payload))
    return urls


def _iter_uncached_card_page_urls(value: Any, key: str | None = None) -> list[tuple[str, str | None]]:
    if isinstance(value, dict):
        urls: list[tuple[str, str | None]] = []
        has_local_page = bool(value.get("local_page"))
        for child_key, child_value in value.items():
            urls.extend(_iter_uncached_card_page_urls(child_value, str(child_key)))
            if not has_local_page and isinstance(child_value, str) and _looks_like_card_page_url(child_value, str(child_key)):
                urls.append((child_value, str(child_key)))
        return urls
    if isinstance(value, list):
        urls = []
        for item in value:
            urls.extend(_iter_uncached_card_page_urls(item, key))
        return urls
    return []


def _find_uncached_card_page_urls(local_message: str) -> list[str]:
    urls: list[tuple[str, str | None]] = []
    for match in _CQ_PATTERN.finditer(local_message):
        if match.group("kind") not in {"json", "xml"}:
            continue
        params = _parse_cq_params(match.group("params"))
        data = params.get("data")
        if not data:
            continue
        try:
            payload = json.loads(html.unescape(data))
        except json.JSONDecodeError:
            continue
        urls.extend(_iter_uncached_card_page_urls(payload))
    return _sort_card_page_candidates(urls)


def _find_uncached_forward_ids(local_message: str) -> list[str]:
    forward_ids: list[str] = []
    for match in _CQ_PATTERN.finditer(local_message):
        if match.group("kind") != "forward":
            continue
        params = _parse_cq_params(match.group("params"))
        forward_id = params.get("id")
        if forward_id and not params.get("local"):
            forward_ids.append(forward_id)
    return forward_ids


def _find_uncached_media_urls(local_message: str) -> list[str]:
    urls = [segment.url for segment in parse_cq_media_segments(local_message)]
    urls.extend(_find_card_media_urls(local_message))
    return urls


def _needs_backfill(local_message: str) -> bool:
    return bool(_find_uncached_forward_ids(local_message) or _find_uncached_media_urls(local_message) or _find_uncached_card_page_urls(local_message))


class MediaBackfillService:
    @staticmethod
    async def backfill_historical_media(
        db: AsyncSession,
        *,
        limit: int = 100,
        dry_run: bool = False,
        failure_limit: int = 20,
        http_client: Any | None = None,
        storage_root: str | None = None,
        public_prefix: str | None = None,
        max_bytes: int | None = None,
        forward_payload_loader: ForwardPayloadLoader | None = None,
        finalize_unavailable: bool = False,
    ) -> MediaBackfillReport:
        report = MediaBackfillReport()
        result = await db.execute(
            select(Message)
            .where(
                or_(
                    Message.local_message.contains("http"),
                    Message.local_message.contains("http://"),
                    Message.local_message.contains("https://"),
                    Message.local_message.contains("http:\\/\\/"),
                    Message.local_message.contains("https:\\/\\/"),
                    Message.local_message.contains("[CQ:forward,"),
                )
            )
            .order_by(Message.timestamp.asc(), Message.msg_hash.asc())
            .limit(limit)
        )
        messages = list(result.scalars().unique().all())

        for message in messages:
            report.scanned += 1
            if not _needs_backfill(message.local_message):
                continue
            report.candidates += 1
            if dry_run:
                report.unchanged += 1
                continue

            before = message.local_message
            before_media_urls = set(_find_uncached_media_urls(before))
            before_card_page_urls = set(_find_uncached_card_page_urls(before))
            before_forward_ids = set(_find_uncached_forward_ids(before))

            rewritten = await MediaService.rewrite_cq_media_to_local_paths(
                db,
                raw_message=before,
                http_client=http_client,
                storage_root=storage_root,
                public_prefix=public_prefix,
                max_bytes=max_bytes,
                unavailable_placeholders=finalize_unavailable,
            )
            if before_forward_ids and forward_payload_loader is not None:
                robot_ids = await MediaBackfillService._robot_ids_for_message(db, message.msg_hash)
                loader = MediaBackfillService._build_forward_loader(robot_ids, forward_payload_loader)
                rewritten = await MediaService.cache_cq_forward_payloads(
                    db,
                    local_message=rewritten,
                    forward_loader=loader,
                    http_client=http_client,
                    storage_root=storage_root,
                    public_prefix=public_prefix,
                    max_bytes=max_bytes,
                    unavailable_placeholders=finalize_unavailable,
                )
            if before_forward_ids and finalize_unavailable:
                rewritten = await MediaBackfillService._finalize_unavailable_forwards(
                    db,
                    rewritten,
                    storage_root=storage_root,
                    public_prefix=public_prefix,
                )

            if rewritten != before:
                message.local_message = rewritten
                report.updated += 1
            else:
                report.unchanged += 1

            after_media_urls = set(_find_uncached_media_urls(rewritten))
            for url in sorted(before_media_urls & after_media_urls):
                report.add_failure(
                    MediaBackfillFailure(message.msg_hash, "media", url, "download_failed_or_expired"),
                    failure_limit,
                )

            after_card_page_urls = set(_find_uncached_card_page_urls(rewritten))
            for url in sorted(before_card_page_urls & after_card_page_urls):
                report.add_failure(
                    MediaBackfillFailure(message.msg_hash, "card_page", url, "snapshot_failed_or_unavailable"),
                    failure_limit,
                )

            after_forward_ids = set(_find_uncached_forward_ids(rewritten))
            for forward_id in sorted(before_forward_ids & after_forward_ids):
                reason = "forward_loader_unavailable" if forward_payload_loader is None else "forward_payload_unavailable"
                report.add_failure(
                    MediaBackfillFailure(message.msg_hash, "forward", forward_id, reason),
                    failure_limit,
                )

        if not dry_run:
            await db.commit()
        return report

    @staticmethod
    async def _robot_ids_for_message(db: AsyncSession, msg_hash: str) -> list[str]:
        result = await db.execute(select(RobotMessage.robot_id).where(RobotMessage.msg_hash == msg_hash).order_by(RobotMessage.robot_id.asc()))
        return [str(robot_id) for robot_id in result.scalars().all()]

    @staticmethod
    def _build_forward_loader(robot_ids: list[str], forward_payload_loader: ForwardPayloadLoader) -> Callable[[str], Awaitable[dict[str, Any]]]:
        async def load_forward(forward_id: str) -> dict[str, Any]:
            last_error: Exception | None = None
            for robot_id in robot_ids:
                try:
                    return await forward_payload_loader(robot_id, forward_id)
                except Exception as exc:
                    last_error = exc
            if last_error is not None:
                raise last_error
            raise LookupError("message has no robot view for forward payload lookup")

        return load_forward

    @staticmethod
    async def _finalize_unavailable_forwards(
        db: AsyncSession,
        local_message: str,
        storage_root: str | None = None,
        public_prefix: str | None = None,
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
            payload = {
                "status": "unavailable",
                "data": {
                    "messages": [
                        {
                            "sender": {"nickname": "本地缓存"},
                            "raw_message": f"合并转发 {forward_id} 未缓存，且当前无法从 OneBot 重新获取。",
                        }
                    ]
                },
            }
            local_path = await MessageService.save_media_asset(
                db,
                file_content=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                file_type="forward_missing",
                ext="json",
                storage_root=storage_root,
                public_prefix=public_prefix,
            )
            params["local"] = local_path
            rewritten = rewritten.replace(match.group(0), _build_cq_segment("forward", params), 1)
        return rewritten
