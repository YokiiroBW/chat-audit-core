import hashlib
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import MediaAsset, Message, RobotMessage


class MessageService:
    @staticmethod
    def generate_md5(content: bytes) -> str:
        return hashlib.md5(content).hexdigest()

    @staticmethod
    async def save_media_asset(
        db: AsyncSession,
        file_content: bytes,
        file_type: str,
        ext: str,
        storage_root: str | Path | None = None,
        public_prefix: str | None = None,
    ) -> str:
        settings = get_settings()
        root = Path(storage_root) if storage_root is not None else settings.storage_root
        prefix = public_prefix if public_prefix is not None else settings.public_storage_prefix
        root.mkdir(parents=True, exist_ok=True)

        clean_ext = ext.lstrip(".").lower()
        file_hash = MessageService.generate_md5(file_content)
        filename = f"{file_hash}.{clean_ext}"
        target_path = root / filename

        if not target_path.exists():
            target_path.write_bytes(file_content)

        local_path = f"{prefix.rstrip('/')}/{filename}"
        result = await db.execute(select(MediaAsset).where(MediaAsset.file_hash == file_hash))
        asset = result.scalar_one_or_none()
        if asset is None:
            db.add(
                MediaAsset(
                    file_hash=file_hash,
                    file_type=file_type,
                    file_size=len(file_content),
                    local_path=local_path,
                )
            )
            await db.commit()

        return local_path

    @staticmethod
    async def process_incoming_message(
        db: AsyncSession,
        robot_id: str,
        platform: str,
        msg_data: dict,
        media_http_client: Any | None = None,
        media_storage_root: str | Path | None = None,
        media_public_prefix: str | None = None,
    ) -> str:
        raw_message = msg_data["raw_message"]
        room_id = msg_data["room_id"]
        sender_id = msg_data["sender_id"]
        event_identity = msg_data.get("message_id")
        if event_identity is None:
            event_identity = f"{msg_data['timestamp']}_{raw_message}"
        raw_string = f"{platform}_{room_id}_{sender_id}_{event_identity}"
        msg_hash = MessageService.generate_md5(raw_string.encode("utf-8"))

        result = await db.execute(select(Message).where(Message.msg_hash == msg_hash))
        existing_msg = result.scalar_one_or_none()
        if existing_msg is None:
            local_message = msg_data.get("local_message", raw_message)
            if media_http_client is not None:
                from app.services.media_service import MediaService

                local_message = await MediaService.rewrite_cq_media_to_local_paths(
                    db,
                    raw_message=raw_message,
                    http_client=media_http_client,
                    storage_root=media_storage_root,
                    public_prefix=media_public_prefix,
                )

            db.add(
                Message(
                    msg_hash=msg_hash,
                    platform=platform,
                    room_id=room_id,
                    message_type=msg_data["message_type"],
                    external_message_id=str(msg_data["message_id"]) if msg_data.get("message_id") is not None else None,
                    sender_id=sender_id,
                    nickname=msg_data.get("nickname"),
                    raw_message=raw_message,
                    local_message=local_message,
                    timestamp=msg_data["timestamp"],
                )
            )

        assoc_result = await db.execute(
            select(RobotMessage).where(
                RobotMessage.robot_id == robot_id,
                RobotMessage.msg_hash == msg_hash,
            )
        )
        if assoc_result.scalar_one_or_none() is None:
            db.add(RobotMessage(robot_id=robot_id, msg_hash=msg_hash))

        await db.commit()
        return msg_hash

    @staticmethod
    async def list_messages(db: AsyncSession) -> list[Message]:
        result = await db.execute(select(Message).order_by(Message.timestamp.asc()))
        return list(result.scalars().all())

    @staticmethod
    async def list_robot_messages(db: AsyncSession) -> list[RobotMessage]:
        result = await db.execute(select(RobotMessage).order_by(RobotMessage.robot_id.asc()))
        return list(result.scalars().all())

    @staticmethod
    async def list_media_assets(db: AsyncSession) -> list[MediaAsset]:
        result = await db.execute(select(MediaAsset).order_by(MediaAsset.file_hash.asc()))
        return list(result.scalars().all())
