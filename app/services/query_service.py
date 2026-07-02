from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Adapter, Message, RobotMessage


class QueryService:
    @staticmethod
    async def list_adapters(db: AsyncSession) -> list[Adapter]:
        result = await db.execute(select(Adapter).order_by(Adapter.id.asc()))
        return list(result.scalars().all())

    @staticmethod
    async def list_rooms(db: AsyncSession, robot_id: str) -> list[dict]:
        result = await db.execute(
            select(
                Message.room_id.label("room_id"),
                func.max(Message.timestamp).label("last_timestamp"),
            )
            .join(RobotMessage, RobotMessage.msg_hash == Message.msg_hash)
            .where(RobotMessage.robot_id == robot_id)
            .group_by(Message.room_id)
            .order_by(desc("last_timestamp"), Message.room_id.asc())
        )
        return [
            {"room_id": row.room_id, "last_timestamp": row.last_timestamp}
            for row in result.all()
        ]

    @staticmethod
    async def list_messages(
        db: AsyncSession,
        robot_id: str,
        room_id: str,
        before_timestamp: int | None = None,
        limit: int = 50,
    ) -> list[Message]:
        stmt = (
            select(Message)
            .join(RobotMessage, RobotMessage.msg_hash == Message.msg_hash)
            .where(RobotMessage.robot_id == robot_id, Message.room_id == room_id)
        )
        if before_timestamp is not None:
            stmt = stmt.where(Message.timestamp < before_timestamp)

        # Cursor loading needs the newest N messages before the cursor, then returns
        # them in chronological order for stable chat rendering.
        stmt = stmt.order_by(Message.timestamp.desc(), Message.msg_hash.desc()).limit(limit)
        result = await db.execute(stmt)
        messages = list(result.scalars().all())
        return list(reversed(messages))
