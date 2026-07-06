from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import MediaAsset, Message, RobotMessage, RoomProfile, UserProfile
from app.services.backup_service import BackupService
from app.services.media_backfill_service import (
    _find_uncached_card_page_urls,
    _find_uncached_forward_ids,
    _find_uncached_media_urls,
)

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
}


@dataclass
class OfflineAuditIssue:
    kind: str
    target: str
    reason: str
    msg_hash: str | None = None
    label: str | None = None
    action: str | None = None

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
    reason_summary: dict[str, int] = field(default_factory=dict)
    issues: list[OfflineAuditIssue] = field(default_factory=list)

    def add_issue(self, issue: OfflineAuditIssue, issue_limit: int) -> None:
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
            stmt = stmt.join(RobotMessage, RobotMessage.msg_hash == Message.msg_hash).where(RobotMessage.robot_id == robot_id)
        if room_id is not None:
            stmt = stmt.where(Message.room_id == room_id)
        stmt = stmt.order_by(Message.timestamp.asc(), Message.msg_hash.asc()).limit(limit)

        result = await db.execute(stmt)
        messages = list(result.scalars().unique().all())
        local_paths: set[str] = set()
        group_room_ids: set[str] = set()
        user_ids: set[str] = set()

        for message in messages:
            report.messages_scanned += 1
            if message.message_type == "group":
                group_room_ids.add(message.room_id)
            elif message.message_type == "private":
                user_ids.add(message.room_id)
            user_ids.add(message.sender_id)
            remote_media_urls = _find_uncached_media_urls(message.local_message)
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

        await OfflineAuditService._audit_profile_avatars(
            db,
            report=report,
            local_paths=local_paths,
            group_room_ids=group_room_ids,
            user_ids=user_ids,
            issue_limit=issue_limit,
            public_storage_prefix=public_storage_prefix,
        )

        asset_result = await db.execute(select(MediaAsset).order_by(MediaAsset.local_path.asc()))
        assets = list(asset_result.scalars().all())
        asset_by_path = {asset.local_path: asset for asset in assets}
        report.media_assets_checked = len(assets)

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
