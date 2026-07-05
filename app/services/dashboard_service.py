from pathlib import Path

from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import BotProfile, MediaAsset, Message, RobotMessage


class DashboardService:
    @staticmethod
    async def get_summary(db: AsyncSession, *, backup_root: str | Path | None = None) -> dict[str, int | str | None]:
        messages = await DashboardService._scalar_int(db, select(func.count(Message.msg_hash)))
        rooms = await DashboardService._scalar_int(db, select(func.count(distinct(Message.room_id))))
        robot_views = await DashboardService._scalar_int(db, select(func.count(RobotMessage.id)))
        bots = await DashboardService._scalar_int(db, select(func.count(BotProfile.id)))
        media_assets = await DashboardService._scalar_int(db, select(func.count(MediaAsset.file_hash)))
        media_bytes = await DashboardService._scalar_int(db, select(func.coalesce(func.sum(MediaAsset.file_size), 0)))

        backups_count = 0
        latest_backup: str | None = None
        if backup_root is not None:
            backup_paths = sorted(
                Path(backup_root).glob("*.json"),
                key=lambda path: (path.stat().st_mtime, path.name),
            )
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
            "backups": backups_count,
            "latest_backup": latest_backup,
        }

    @staticmethod
    async def _scalar_int(db: AsyncSession, stmt) -> int:
        result = await db.execute(stmt)
        return int(result.scalar_one() or 0)
