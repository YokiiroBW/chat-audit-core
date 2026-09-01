from pathlib import Path

from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.backup.jobs import list_completed_backups
from app.models import BotProfile, MediaAsset, Message, MessageMediaReference, RobotMessage


class DashboardService:
    @staticmethod
    async def get_summary(db: AsyncSession, *, backup_root: str | Path | None = None) -> dict[str, int | str | None]:
        messages = await DashboardService._scalar_int(db, select(func.count(Message.msg_hash)))
        rooms = await DashboardService._scalar_int(db, select(func.count(distinct(Message.room_id))))
        robot_views = await DashboardService._scalar_int(db, select(func.count(RobotMessage.id)))
        bots = await DashboardService._scalar_int(db, select(func.count(BotProfile.id)))
        media_assets = await DashboardService._scalar_int(db, select(func.count(MediaAsset.file_hash)))
        media_bytes = await DashboardService._scalar_int(db, select(func.coalesce(func.sum(MediaAsset.file_size), 0)))
        not_downloaded_media = await DashboardService._scalar_int(
            db,
            select(func.count(MessageMediaReference.id)).where(MessageMediaReference.source_state == "not_downloaded"),
        )
        not_downloaded_videos = await DashboardService._scalar_int(
            db,
            select(func.count(MessageMediaReference.id)).where(
                MessageMediaReference.media_type == "video",
                MessageMediaReference.source_state == "not_downloaded",
            ),
        )
        thumbnail_only_videos = await DashboardService._scalar_int(
            db,
            select(func.count(MessageMediaReference.id)).where(
                MessageMediaReference.media_type == "video",
                MessageMediaReference.archive_state == "thumbnail_only",
            ),
        )
        source_missing_media = await DashboardService._scalar_int(
            db,
            select(func.count(MessageMediaReference.id)).where(MessageMediaReference.source_state == "missing"),
        )
        media_parse_failures = await DashboardService._scalar_int(
            db,
            select(func.count(MessageMediaReference.id)).where(
                MessageMediaReference.source_state == "unknown",
                MessageMediaReference.archive_state == "failed",
            ),
        )

        backups_count = 0
        latest_backup: str | None = None
        if backup_root is not None:
            backup_paths = list_completed_backups(Path(backup_root))
            backups_count = len(backup_paths)
            if backup_paths:
                latest_backup = backup_paths[-1].name

        return {
            "bots": bots,
            "rooms": rooms,
            "messages": messages,
            "robot_views": robot_views,
            "media_assets": media_assets,
            "media_bytes": media_bytes,
            "not_downloaded_media": not_downloaded_media,
            "not_downloaded_videos": not_downloaded_videos,
            "thumbnail_only_videos": thumbnail_only_videos,
            "source_missing_media": source_missing_media,
            "media_parse_failures": media_parse_failures,
            "backups": backups_count,
            "latest_backup": latest_backup,
        }

    @staticmethod
    async def _scalar_int(db: AsyncSession, stmt) -> int:
        result = await db.execute(stmt)
        return int(result.scalar_one() or 0)
