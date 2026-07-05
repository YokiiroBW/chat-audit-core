from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import MediaAsset, Message, RobotMessage
from app.services.backup_service import BackupService
from app.services.media_backfill_service import (
    _find_uncached_card_page_urls,
    _find_uncached_forward_ids,
    _find_uncached_media_urls,
)


@dataclass
class OfflineAuditIssue:
    kind: str
    target: str
    reason: str
    msg_hash: str | None = None


@dataclass
class OfflineAuditReport:
    offline_ready: bool = True
    messages_scanned: int = 0
    media_assets_checked: int = 0
    remote_media_urls: int = 0
    uncached_card_pages: int = 0
    uncached_forwards: int = 0
    missing_media_assets: int = 0
    missing_media_files: int = 0
    issues: list[OfflineAuditIssue] = field(default_factory=list)

    def add_issue(self, issue: OfflineAuditIssue, issue_limit: int) -> None:
        self.offline_ready = False
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

        for message in messages:
            report.messages_scanned += 1
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
