import re

from sqlalchemy import desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Adapter, BotProfile, Message, RobotMessage, RoomProfile, UserProfile


_REPLY_PATTERN = re.compile(r"\[CQ:reply,([^\]]+)\]")


def _parse_reply_id(value: str | None) -> str | None:
    if not value:
        return None
    match = _REPLY_PATTERN.search(value)
    if not match:
        return None
    for item in match.group(1).split(","):
        if item.startswith("id="):
            return item.split("=", 1)[1]
    return None


def _plain_message_preview(value: str | None) -> str:
    text = value or ""
    text = re.sub(r"\[CQ:reply,[^\]]+\]", "", text)
    text = re.sub(r"\[CQ:at,qq=([^\],]+)[^\]]*\]", r"@\1", text)
    text = re.sub(r"\[CQ:image,[^\]]+\]", "[图片]", text)
    text = re.sub(r"\[CQ:record,[^\]]+\]", "[语音]", text)
    text = re.sub(r"\[CQ:video,[^\]]+\]", "[视频]", text)
    text = re.sub(r"\[CQ:forward,[^\]]+\]", "[合并转发]", text)
    text = re.sub(r"\[CQ:json,[^\]]+\]", "[卡片]", text)
    text = re.sub(r"/static/storage/[a-f0-9]{32}\.[a-z0-9]+", "[媒体]", text, flags=re.I)
    compact = re.sub(r"\s+", " ", text).strip()
    return compact or "[消息]"


class QueryService:
    @staticmethod
    async def list_adapters(db: AsyncSession) -> list[Adapter]:
        result = await db.execute(select(Adapter).order_by(Adapter.id.asc()))
        return list(result.scalars().all())

    @staticmethod
    async def list_bot_profiles(db: AsyncSession) -> list[BotProfile]:
        result = await db.execute(select(BotProfile).order_by(BotProfile.last_seen_at.desc(), BotProfile.id.asc()))
        return list(result.scalars().all())

    @staticmethod
    async def list_rooms(db: AsyncSession, robot_id: str) -> list[dict]:
        result = await db.execute(
            select(
                Message.room_id.label("room_id"),
                func.max(Message.timestamp).label("last_timestamp"),
                func.max(Message.message_type).label("message_type"),
                func.max(func.coalesce(RoomProfile.display_name, UserProfile.display_name)).label("display_name"),
                func.max(func.coalesce(RoomProfile.avatar_path, UserProfile.avatar_path)).label("avatar_path"),
            )
            .join(RobotMessage, RobotMessage.msg_hash == Message.msg_hash)
            .outerjoin(RoomProfile, RoomProfile.room_id == Message.room_id)
            .outerjoin(UserProfile, UserProfile.user_id == Message.room_id)
            .where(RobotMessage.robot_id == robot_id)
            .group_by(Message.room_id)
            .order_by(desc("last_timestamp"), Message.room_id.asc())
        )
        return [
            {
                "room_id": row.room_id,
                "last_timestamp": row.last_timestamp,
                "message_type": row.message_type,
                "display_name": row.display_name,
                "avatar_path": row.avatar_path,
            }
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
            select(
                Message,
                UserProfile.display_name.label("sender_display_name"),
                UserProfile.avatar_path.label("sender_avatar_path"),
            )
            .join(RobotMessage, RobotMessage.msg_hash == Message.msg_hash)
            .outerjoin(UserProfile, UserProfile.user_id == Message.sender_id)
            .where(RobotMessage.robot_id == robot_id, Message.room_id == room_id)
        )
        if before_timestamp is not None:
            stmt = stmt.where(Message.timestamp < before_timestamp)

        # Cursor loading needs the newest N messages before the cursor, then returns
        # them in chronological order for stable chat rendering.
        stmt = stmt.order_by(Message.timestamp.desc(), Message.msg_hash.desc()).limit(limit)
        result = await db.execute(stmt)
        messages = []
        for message, sender_display_name, sender_avatar_path in result.all():
            message.sender_display_name = sender_display_name
            message.sender_avatar_path = sender_avatar_path
            messages.append(message)
        messages = list(reversed(messages))
        await QueryService._attach_reply_previews(db, messages)
        return messages

    @staticmethod
    async def _attach_reply_previews(db: AsyncSession, messages: list[Message]) -> None:
        reply_ids = {
            reply_id
            for message in messages
            if (reply_id := _parse_reply_id(message.local_message or message.raw_message))
        }
        if not reply_ids:
            return
        result = await db.execute(select(Message).where(Message.external_message_id.in_(reply_ids)))
        by_external_id = {message.external_message_id: message for message in result.scalars().all() if message.external_message_id}
        for message in messages:
            reply_id = _parse_reply_id(message.local_message or message.raw_message)
            message.reply_to_message_id = reply_id
            source = by_external_id.get(reply_id or "")
            if source is not None:
                sender = source.nickname or source.sender_id
                message.reply_preview_text = f"{sender}: {_plain_message_preview(source.local_message or source.raw_message)}"

    @staticmethod
    async def search_messages(
        db: AsyncSession,
        robot_id: str,
        keyword: str | None = None,
        room_id: str | None = None,
        sender_id: str | None = None,
        start_timestamp: int | None = None,
        end_timestamp: int | None = None,
        limit: int = 50,
    ) -> list[Message]:
        stmt = (
            select(Message)
            .join(RobotMessage, RobotMessage.msg_hash == Message.msg_hash)
            .where(RobotMessage.robot_id == robot_id)
        )
        if keyword:
            pattern = f"%{keyword}%"
            stmt = stmt.where(
                or_(
                    Message.raw_message.like(pattern),
                    Message.local_message.like(pattern),
                    Message.nickname.like(pattern),
                )
            )
        if room_id:
            stmt = stmt.where(Message.room_id == room_id)
        if sender_id:
            stmt = stmt.where(Message.sender_id == sender_id)
        if start_timestamp is not None:
            stmt = stmt.where(Message.timestamp >= start_timestamp)
        if end_timestamp is not None:
            stmt = stmt.where(Message.timestamp <= end_timestamp)

        stmt = stmt.order_by(Message.timestamp.desc(), Message.msg_hash.desc()).limit(limit)
        result = await db.execute(stmt)
        return list(result.scalars().unique().all())
