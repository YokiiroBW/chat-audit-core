import datetime as dt
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import MediaAsset, Message, RobotMessage

BACKUP_SCHEMA = "chat-audit-core.backup.v1"


class BackupService:
    @staticmethod
    def _message_to_dict(message: Message) -> dict[str, Any]:
        return {
            "msg_hash": message.msg_hash,
            "platform": message.platform,
            "room_id": message.room_id,
            "message_type": message.message_type,
            "sender_id": message.sender_id,
            "nickname": message.nickname,
            "raw_message": message.raw_message,
            "local_message": message.local_message,
            "timestamp": message.timestamp,
        }

    @staticmethod
    def _media_asset_to_dict(asset: MediaAsset) -> dict[str, Any]:
        return {
            "file_hash": asset.file_hash,
            "file_type": asset.file_type,
            "file_size": asset.file_size,
            "local_path": asset.local_path,
        }

    @staticmethod
    async def export_package(
        db: AsyncSession,
        robot_id: str | None = None,
        room_id: str | None = None,
        start_timestamp: int | None = None,
        end_timestamp: int | None = None,
    ) -> dict[str, Any]:
        stmt = select(Message)
        if robot_id is not None:
            stmt = stmt.join(RobotMessage, RobotMessage.msg_hash == Message.msg_hash).where(RobotMessage.robot_id == robot_id)
        if room_id is not None:
            stmt = stmt.where(Message.room_id == room_id)
        if start_timestamp is not None:
            stmt = stmt.where(Message.timestamp >= start_timestamp)
        if end_timestamp is not None:
            stmt = stmt.where(Message.timestamp <= end_timestamp)
        stmt = stmt.order_by(Message.timestamp.asc(), Message.msg_hash.asc())

        result = await db.execute(stmt)
        messages = list(result.scalars().unique().all())
        msg_hashes = [message.msg_hash for message in messages]

        robot_messages: list[dict[str, str]] = []
        if msg_hashes:
            assoc_stmt = select(RobotMessage).where(RobotMessage.msg_hash.in_(msg_hashes))
            if robot_id is not None:
                assoc_stmt = assoc_stmt.where(RobotMessage.robot_id == robot_id)
            assoc_result = await db.execute(assoc_stmt.order_by(RobotMessage.robot_id.asc(), RobotMessage.msg_hash.asc()))
            robot_messages = [
                {"robot_id": assoc.robot_id, "msg_hash": assoc.msg_hash}
                for assoc in assoc_result.scalars().all()
            ]

        media_paths = sorted(
            {
                message.local_message
                for message in messages
                if isinstance(message.local_message, str) and "/static/storage/" in message.local_message
            }
        )
        media_assets: list[dict[str, Any]] = []
        if media_paths:
            media_result = await db.execute(select(MediaAsset).where(MediaAsset.local_path.in_(media_paths)).order_by(MediaAsset.file_hash.asc()))
            media_assets = [BackupService._media_asset_to_dict(asset) for asset in media_result.scalars().all()]

        return {
            "manifest": {
                "schema": BACKUP_SCHEMA,
                "created_at": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
                "filters": {
                    "robot_id": robot_id,
                    "room_id": room_id,
                    "start_timestamp": start_timestamp,
                    "end_timestamp": end_timestamp,
                },
                "counts": {
                    "messages": len(messages),
                    "robot_messages": len(robot_messages),
                    "media_assets": len(media_assets),
                },
            },
            "messages": [BackupService._message_to_dict(message) for message in messages],
            "robot_messages": robot_messages,
            "media_assets": media_assets,
        }

    @staticmethod
    async def import_package(db: AsyncSession, package: dict[str, Any]) -> dict[str, int]:
        manifest = package.get("manifest") or {}
        schema = manifest.get("schema")
        if schema != BACKUP_SCHEMA:
            raise ValueError(f"unsupported backup schema: {schema!r}")

        message_count = 0
        for item in package.get("messages", []):
            result = await db.execute(select(Message).where(Message.msg_hash == item["msg_hash"]))
            message = result.scalar_one_or_none()
            if message is None:
                db.add(
                    Message(
                        msg_hash=item["msg_hash"],
                        platform=item["platform"],
                        room_id=item["room_id"],
                        message_type=item["message_type"],
                        sender_id=item["sender_id"],
                        nickname=item.get("nickname"),
                        raw_message=item["raw_message"],
                        local_message=item["local_message"],
                        timestamp=item["timestamp"],
                    )
                )
            else:
                message.platform = item["platform"]
                message.room_id = item["room_id"]
                message.message_type = item["message_type"]
                message.sender_id = item["sender_id"]
                message.nickname = item.get("nickname")
                message.raw_message = item["raw_message"]
                message.local_message = item["local_message"]
                message.timestamp = item["timestamp"]
            message_count += 1

        robot_message_count = 0
        for item in package.get("robot_messages", []):
            result = await db.execute(
                select(RobotMessage).where(
                    RobotMessage.robot_id == item["robot_id"],
                    RobotMessage.msg_hash == item["msg_hash"],
                )
            )
            assoc = result.scalar_one_or_none()
            if assoc is None:
                db.add(RobotMessage(robot_id=item["robot_id"], msg_hash=item["msg_hash"]))
            robot_message_count += 1

        media_asset_count = 0
        for item in package.get("media_assets", []):
            result = await db.execute(select(MediaAsset).where(MediaAsset.file_hash == item["file_hash"]))
            asset = result.scalar_one_or_none()
            if asset is None:
                db.add(
                    MediaAsset(
                        file_hash=item["file_hash"],
                        file_type=item["file_type"],
                        file_size=item["file_size"],
                        local_path=item["local_path"],
                    )
                )
            else:
                asset.file_type = item["file_type"]
                asset.file_size = item["file_size"]
                asset.local_path = item["local_path"]
            media_asset_count += 1

        await db.commit()
        return {
            "messages": message_count,
            "robot_messages": robot_message_count,
            "media_assets": media_asset_count,
        }
