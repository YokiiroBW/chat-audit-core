from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.message_scope import robot_message_join, robot_message_scope
from app.models import Message, RobotMessage


class AmbiguousConversationError(ValueError):
    """Raised when a legacy room-id-only request matches multiple conversations."""


async def resolve_conversation_message_type(
    db: AsyncSession,
    *,
    room_id: str,
    robot_id: str | None = None,
    message_type: str | None = None,
) -> str | None:
    if message_type is not None:
        return message_type

    stmt = select(Message.message_type).where(Message.room_id == room_id).distinct().limit(2)
    if robot_id is not None:
        stmt = stmt.outerjoin(RobotMessage, robot_message_join(robot_id)).where(robot_message_scope(robot_id))
    result = await db.execute(stmt)
    message_types = list(result.scalars().all())
    if len(message_types) > 1:
        raise AmbiguousConversationError(
            f"room_id {room_id!r} identifies multiple conversation types; message_type is required"
        )
    return message_types[0] if message_types else None
