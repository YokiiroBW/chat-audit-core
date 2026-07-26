from dataclasses import dataclass, field
import hashlib
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.message_scope import apply_robot_message_scope
from app.models import (
    MediaAsset,
    Message,
    MessageMediaReference,
    MessageSourceRecord,
    RoomProfile,
    UserProfile,
)
from app.services.backup_service import BackupService
from app.services.media_backfill_service import (
    _find_uncached_card_page_urls,
    _find_uncached_forward_ids,
    _find_uncached_media_urls,
)
from app.services.media_service import _parse_cq_media_segments_with_ordinals

OFFLINE_ISSUE_DETAILS = {
    "message_still_references_remote_media": (
        "消息仍引用远程媒体地址",
        "运行媒体回填；如果源端地址已过期，可用占位文件封存缺失原因。",
    ),
    "card_page_snapshot_missing": (
        "卡片网页快照未缓存",
        "运行媒体回填缓存卡片网页；如果页面不可达，可封存为缺失快照。",
    ),
    "forward_payload_not_cached": (
        "合并转发详情未缓存",
        "在机器人在线时运行媒体回填，拉取并缓存合并转发子消息。",
    ),
    "profile_avatar_not_cached": (
        "头像未缓存",
        "运行离线修复，生成占位头像或重新尝试拉取头像。",
    ),
    "profile_avatar_not_local": (
        "头像仍指向非本地地址",
        "运行离线修复，将头像转成本地缓存路径。",
    ),
    "local_path_has_no_media_asset_index": (
        "本地路径缺少媒体索引",
        "运行离线修复，为已存在的本地文件补建媒体索引。",
    ),
    "media_asset_file_missing": (
        "媒体索引对应文件丢失",
        "运行离线修复创建缺失占位文件，或重新回填原始媒体。",
    ),
    "media_asset_hash_mismatch": ("媒体文件哈希不一致", "从可信备份恢复原始文件，禁止生成占位文件。"),
    "media_asset_empty": ("媒体文件为空", "重新归档真实媒体文件，零字节文件不能视为完整资产。"),
    "source_media_missing": ("源媒体文件已丢失", "源记录显示文件曾存在，但当前源设备文件不可用。"),
    "media_parse_failed": ("媒体记录解析失败", "保留原始记录并在解析器升级后重新处理。"),
    "media_archive_failed": ("媒体归档失败", "检查源文件读取、校验和 NAS 上传错误后重试。"),
    "media_reference_asset_missing": ("媒体引用资产不存在", "检查上传确认和媒体引用绑定流程。"),
}


@dataclass
class OfflineAuditIssue:
    kind: str
    target: str
    reason: str
    msg_hash: str | None = None
    label: str | None = None
    action: str | None = None
    severity: str = "error"
    affects_readiness: bool = True

    def __post_init__(self) -> None:
        if self.label is not None and self.action is not None:
            return
        label, action = OFFLINE_ISSUE_DETAILS.get(self.reason, ("未缓存资产", "检查该条记录并按需运行媒体回填或离线修复。"))
        if self.label is None:
            self.label = label
        if self.action is None:
            self.action = action


@dataclass
class OfflineAuditReport:
    offline_ready: bool = True
    messages_scanned: int = 0
    media_assets_checked: int = 0
    profile_avatars_checked: int = 0
    remote_media_urls: int = 0
    uncached_card_pages: int = 0
    uncached_forwards: int = 0
    missing_profile_avatars: int = 0
    missing_media_assets: int = 0
    missing_media_files: int = 0
    not_downloaded_media: int = 0
    not_downloaded_videos: int = 0
    thumbnail_only_videos: int = 0
    source_missing_media: int = 0
    media_parse_failures: int = 0
    media_hash_mismatches: int = 0
    reason_summary: dict[str, int] = field(default_factory=dict)
    issues: list[OfflineAuditIssue] = field(default_factory=list)

    def add_issue(self, issue: OfflineAuditIssue, issue_limit: int) -> None:
        if issue.affects_readiness:
            self.offline_ready = False
        self.reason_summary[issue.reason] = self.reason_summary.get(issue.reason, 0) + 1
        if len(self.issues) < issue_limit:
            self.issues.append(issue)


class OfflineAuditService:
    @staticmethod
    async def audit_offline_readiness(
        db: AsyncSession,
        *,
        robot_id: str | None = None,
        room_id: str | None = None,
        limit: int = 5000,
        issue_limit: int = 100,
        storage_root: str | Path | None = None,
        public_storage_prefix: str = "/static/storage",
    ) -> OfflineAuditReport:
        report = OfflineAuditReport()
        stmt = select(Message)
        if robot_id is not None:
            # Same account-visibility rule as the query layer: an inner join on
            # robot_messages alone hides QQNT-imported conversations, which have
            # no robot_messages row, so the audit silently reported them clean.
            stmt = apply_robot_message_scope(stmt, robot_id)
        if room_id is not None:
            stmt = stmt.where(Message.room_id == room_id)
        stmt = stmt.order_by(Message.timestamp.asc(), Message.msg_hash.asc()).limit(limit)

        result = await db.execute(stmt)
        messages = list(result.scalars().unique().all())
        msg_hashes = [message.msg_hash for message in messages]
        references_by_message: dict[str, list[MessageMediaReference]] = {msg_hash: [] for msg_hash in msg_hashes}
        source_record_messages: set[str] = set()
        if msg_hashes:
            reference_result = await db.execute(
                select(MessageMediaReference)
                .where(MessageMediaReference.msg_hash.in_(msg_hashes))
                .order_by(MessageMediaReference.msg_hash.asc(), MessageMediaReference.ordinal.asc())
            )
            for reference in reference_result.scalars().all():
                references_by_message[reference.msg_hash].append(reference)
            source_record_result = await db.execute(
                select(MessageSourceRecord.msg_hash).where(MessageSourceRecord.msg_hash.in_(msg_hashes)).distinct()
            )
            source_record_messages = set(source_record_result.scalars().all())

        local_paths: set[str] = set()
        referenced_asset_hashes: set[str] = set()
        group_room_ids: set[str] = set()
        user_ids: set[str] = set()

        for message in messages:
            report.messages_scanned += 1
            if message.message_type == "group":
                group_room_ids.add(message.room_id)
            elif message.message_type == "private":
                user_ids.add(message.room_id)
            user_ids.add(message.sender_id)
            references = references_by_message.get(message.msg_hash, [])
            remote_media_urls = _find_uncached_media_urls(message.local_message)
            if references and remote_media_urls:
                # Reference ordinals are slots over *all* media in the message,
                # archived or not, so they cannot index a list that skips the
                # archived ones: once any media has been localised the positions
                # shift and the wrong URL gets marked as covered.
                url_by_ordinal = {
                    ordinal: segment.url
                    for ordinal, segment in _parse_cq_media_segments_with_ordinals(message.local_message, public_storage_prefix)
                }
                covered_urls = {
                    url
                    for reference in references
                    if (url := url_by_ordinal.get(reference.ordinal)) is not None
                }
                remote_media_urls = [url for url in remote_media_urls if url not in covered_urls]
            card_page_urls = _find_uncached_card_page_urls(message.local_message)
            forward_ids = _find_uncached_forward_ids(message.local_message)

            report.remote_media_urls += len(remote_media_urls)
            report.uncached_card_pages += len(card_page_urls)
            report.uncached_forwards += len(forward_ids)
            local_paths.update(BackupService._extract_local_media_paths(message.local_message, public_storage_prefix))

            for url in remote_media_urls:
                report.add_issue(OfflineAuditIssue("remote_media", url, "message_still_references_remote_media", message.msg_hash), issue_limit)
            for url in card_page_urls:
                report.add_issue(OfflineAuditIssue("card_page", url, "card_page_snapshot_missing", message.msg_hash), issue_limit)
            for forward_id in forward_ids:
                report.add_issue(OfflineAuditIssue("forward", forward_id, "forward_payload_not_cached", message.msg_hash), issue_limit)

            for reference in references:
                if reference.source_state == "not_downloaded":
                    report.not_downloaded_media += 1
                    if reference.media_type == "video":
                        report.not_downloaded_videos += 1
                if reference.media_type == "video" and reference.archive_state == "thumbnail_only":
                    report.thumbnail_only_videos += 1
                if reference.source_state == "missing":
                    report.source_missing_media += 1
                    report.add_issue(
                        OfflineAuditIssue(
                            "source_media",
                            f"{message.msg_hash}:{reference.ordinal}",
                            "source_media_missing",
                            message.msg_hash,
                        ),
                        issue_limit,
                    )
                if reference.archive_state == "failed":
                    if reference.source_state == "unknown":
                        report.media_parse_failures += 1
                        replayable = message.msg_hash in source_record_messages
                        report.add_issue(
                            OfflineAuditIssue(
                                "media_parse",
                                f"{message.msg_hash}:{reference.ordinal}",
                                "media_parse_failed",
                                message.msg_hash,
                                severity="warning" if replayable else "error",
                                affects_readiness=not replayable,
                            ),
                            issue_limit,
                        )
                    else:
                        report.add_issue(
                            OfflineAuditIssue(
                                "media_archive",
                                f"{message.msg_hash}:{reference.ordinal}",
                                "media_archive_failed",
                                message.msg_hash,
                            ),
                            issue_limit,
                        )
                if reference.asset_file_hash:
                    referenced_asset_hashes.add(reference.asset_file_hash)
                if reference.thumbnail_file_hash:
                    referenced_asset_hashes.add(reference.thumbnail_file_hash)
                if reference.archive_state == "complete" and not reference.asset_file_hash:
                    report.missing_media_assets += 1
                    report.add_issue(
                        OfflineAuditIssue(
                            "media_reference",
                            f"{message.msg_hash}:{reference.ordinal}",
                            "media_reference_asset_missing",
                            message.msg_hash,
                        ),
                        issue_limit,
                    )
                if reference.archive_state == "thumbnail_only" and not reference.thumbnail_file_hash:
                    report.missing_media_assets += 1
                    report.add_issue(
                        OfflineAuditIssue(
                            "media_reference",
                            f"{message.msg_hash}:{reference.ordinal}:thumbnail",
                            "media_reference_asset_missing",
                            message.msg_hash,
                        ),
                        issue_limit,
                    )

        await OfflineAuditService._audit_profile_avatars(
            db,
            report=report,
            local_paths=local_paths,
            group_room_ids=group_room_ids,
            user_ids=user_ids,
            issue_limit=issue_limit,
            public_storage_prefix=public_storage_prefix,
        )

        asset_stmt = select(MediaAsset)
        if robot_id is not None or room_id is not None:
            legacy_asset_paths = set(local_paths)
            conditions = []
            if referenced_asset_hashes:
                conditions.append(MediaAsset.file_hash.in_(referenced_asset_hashes))
            if legacy_asset_paths:
                conditions.append(MediaAsset.local_path.in_(legacy_asset_paths))
            if conditions:
                from sqlalchemy import or_

                asset_stmt = asset_stmt.where(or_(*conditions))
            else:
                asset_stmt = asset_stmt.where(MediaAsset.file_hash == "")
        asset_result = await db.execute(asset_stmt.order_by(MediaAsset.local_path.asc()))
        assets = list(asset_result.scalars().all())
        asset_by_path = {asset.local_path: asset for asset in assets}
        asset_by_hash = {asset.file_hash: asset for asset in assets}
        report.media_assets_checked = len(assets)

        for file_hash in sorted(referenced_asset_hashes):
            if file_hash not in asset_by_hash:
                report.missing_media_assets += 1
                report.add_issue(OfflineAuditIssue("media_asset", file_hash, "media_reference_asset_missing"), issue_limit)

        for local_path in sorted(local_paths):
            if local_path not in asset_by_path:
                report.missing_media_assets += 1
                report.add_issue(OfflineAuditIssue("media_asset", local_path, "local_path_has_no_media_asset_index"), issue_limit)

        if storage_root is not None:
            root = Path(storage_root)
            for asset in assets:
                file_path = BackupService._local_media_file_path(asset.local_path, root, public_storage_prefix)
                if file_path is None or not file_path.exists():
                    report.missing_media_files += 1
                    report.add_issue(OfflineAuditIssue("media_file", asset.local_path, "media_asset_file_missing"), issue_limit)
                    continue
                if not file_path.stat().st_size:
                    report.missing_media_files += 1
                    report.add_issue(OfflineAuditIssue("media_file", asset.local_path, "media_asset_empty"), issue_limit)
                    continue
                is_hex_hash = all(char in "0123456789abcdefABCDEF" for char in asset.file_hash)
                if is_hex_hash and len(asset.file_hash) in {32, 64}:
                    # Chunked: archived media runs to media_max_bytes, 100 MB by
                    # default, and reading each file whole made an audit scale
                    # with the largest file rather than with a fixed buffer.
                    with file_path.open("rb") as handle:
                        actual_hash = hashlib.file_digest(handle, "md5" if len(asset.file_hash) == 32 else "sha256").hexdigest()
                    if actual_hash.lower() != asset.file_hash.lower():
                        report.media_hash_mismatches += 1
                        report.add_issue(OfflineAuditIssue("media_file", asset.local_path, "media_asset_hash_mismatch"), issue_limit)

        return report

    @staticmethod
    async def _audit_profile_avatars(
        db: AsyncSession,
        *,
        report: OfflineAuditReport,
        local_paths: set[str],
        group_room_ids: set[str],
        user_ids: set[str],
        issue_limit: int,
        public_storage_prefix: str,
    ) -> None:
        if group_room_ids:
            room_result = await db.execute(select(RoomProfile).where(RoomProfile.room_id.in_(group_room_ids)))
            room_profiles = {profile.room_id: profile for profile in room_result.scalars().all()}
            for room_id in sorted(group_room_ids):
                report.profile_avatars_checked += 1
                avatar_path = (room_profiles.get(room_id) or RoomProfile(room_id=room_id, platform="qq")).avatar_path
                OfflineAuditService._audit_profile_avatar_path(
                    report,
                    target=f"room:{room_id}",
                    avatar_path=avatar_path,
                    local_paths=local_paths,
                    issue_limit=issue_limit,
                    public_storage_prefix=public_storage_prefix,
                )

        if user_ids:
            user_result = await db.execute(select(UserProfile).where(UserProfile.user_id.in_(user_ids)))
            user_profiles = {profile.user_id: profile for profile in user_result.scalars().all()}
            for user_id in sorted(user_ids):
                report.profile_avatars_checked += 1
                avatar_path = (user_profiles.get(user_id) or UserProfile(user_id=user_id, platform="qq")).avatar_path
                OfflineAuditService._audit_profile_avatar_path(
                    report,
                    target=f"user:{user_id}",
                    avatar_path=avatar_path,
                    local_paths=local_paths,
                    issue_limit=issue_limit,
                    public_storage_prefix=public_storage_prefix,
                )

    @staticmethod
    def _audit_profile_avatar_path(
        report: OfflineAuditReport,
        *,
        target: str,
        avatar_path: str | None,
        local_paths: set[str],
        issue_limit: int,
        public_storage_prefix: str,
    ) -> None:
        prefix = public_storage_prefix.rstrip("/") + "/"
        if not avatar_path:
            report.missing_profile_avatars += 1
            report.add_issue(OfflineAuditIssue("profile_avatar", target, "profile_avatar_not_cached"), issue_limit)
            return
        if not avatar_path.startswith(prefix):
            report.missing_profile_avatars += 1
            report.add_issue(OfflineAuditIssue("profile_avatar", avatar_path, "profile_avatar_not_local"), issue_limit)
            return
        local_paths.add(avatar_path)
