import hashlib
import json
from pathlib import Path
import re
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import MediaAsset, Message, MessageMediaReference, MessagePart, MessageSourceRecord, RobotMessage
from app.services.capture_policy_service import CapturePolicyService
from app.time_utils import utc_now


class MessageService:
    @staticmethod
    def generate_md5(content: bytes) -> str:
        return hashlib.md5(content).hexdigest()

    @staticmethod
    def message_hash(*, platform: str, room_id: str, sender_id: str, event_identity: str) -> str:
        raw_string = f"{platform}_{room_id}_{sender_id}_{event_identity}"
        return MessageService.generate_md5(raw_string.encode("utf-8"))

    @staticmethod
    async def resolve_existing_message(
        db: AsyncSession,
        *,
        platform: str,
        room_id: str,
        sender_id: str,
        event_identity: str,
    ) -> tuple[str, Message | None]:
        msg_hash = MessageService.message_hash(
            platform=platform,
            room_id=room_id,
            sender_id=sender_id,
            event_identity=event_identity,
        )
        result = await db.execute(select(Message).where(Message.msg_hash == msg_hash).with_for_update())
        message = result.scalar_one_or_none()
        if message is None:
            result = await db.execute(
                select(Message).where(
                    Message.platform == platform,
                    Message.room_id == room_id,
                    Message.sender_id == sender_id,
                    Message.external_message_id == event_identity,
                ).with_for_update()
            )
            message = result.scalars().first()
        if message is None:
            result = await db.execute(
                select(Message)
                .join(MessageSourceRecord, MessageSourceRecord.msg_hash == Message.msg_hash)
                .where(
                    Message.platform == platform,
                    Message.room_id == room_id,
                    Message.sender_id == sender_id,
                    or_(
                        MessageSourceRecord.source_external_message_id == event_identity,
                        MessageSourceRecord.platform_message_id == event_identity,
                    ),
                )
                .with_for_update()
            )
            message = result.scalars().first()
        return (message.msg_hash if message is not None else msg_hash), message

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
        content_sha256 = hashlib.sha256(file_content).hexdigest()
        existing_by_sha256 = await db.execute(select(MediaAsset).where(MediaAsset.content_sha256 == content_sha256))
        existing_asset = existing_by_sha256.scalar_one_or_none()
        if existing_asset is not None:
            return existing_asset.local_path
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
                    content_sha256=content_sha256,
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
        forward_payload_loader: Any | None = None,
    ) -> str | None:
        capture_decision = await CapturePolicyService.should_capture(db, robot_id=robot_id, msg_data=msg_data)
        if not capture_decision.should_capture:
            return None

        raw_message = msg_data["raw_message"]
        room_id = msg_data["room_id"]
        sender_id = msg_data["sender_id"]
        event_identity = msg_data.get("message_id")
        if event_identity is None:
            event_identity = f"{msg_data['timestamp']}_{raw_message}"
        msg_hash, existing_msg = await MessageService.resolve_existing_message(
            db,
            platform=platform,
            room_id=room_id,
            sender_id=sender_id,
            event_identity=str(event_identity),
        )

        local_message = msg_data.get("local_message", raw_message)
        if media_http_client is not None:
            from app.services.media_service import MediaService

            local_message = await MediaService.rewrite_cq_media_to_local_paths(
                db,
                raw_message=raw_message,
                http_client=media_http_client,
                storage_root=media_storage_root,
                public_prefix=media_public_prefix,
                allowed_media_types=capture_decision.allowed_media_types,
            )
            if forward_payload_loader is not None:
                local_message = await MediaService.cache_cq_forward_payloads(
                    db,
                    local_message=local_message,
                    forward_loader=forward_payload_loader,
                    http_client=media_http_client,
                    storage_root=media_storage_root,
                    public_prefix=media_public_prefix,
                    allowed_media_types=capture_decision.allowed_media_types,
                    forward_depth=get_settings().forward_cache_max_depth,
                )

        if existing_msg is not None:
            relock_result = await db.execute(select(Message).where(Message.msg_hash == msg_hash).with_for_update())
            existing_msg = relock_result.scalar_one()

        if existing_msg is None:

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
                    is_outgoing=msg_data.get("is_outgoing"),
                )
            )
        else:
            if msg_data.get("is_outgoing") is not None:
                existing_msg.is_outgoing = msg_data.get("is_outgoing")
            if msg_data.get("nickname") and (not existing_msg.nickname or msg_data.get("is_outgoing")):
                existing_msg.nickname = msg_data.get("nickname")
            if MessageService._local_message_score(local_message) > MessageService._local_message_score(existing_msg.local_message):
                existing_msg.local_message = local_message

        await MessageService._upsert_message_parts(
            db, msg_hash=msg_hash, segments=msg_data.get("message_segments"), source_format=msg_data.get("source_event_type") or platform,
        )

        await MessageService._upgrade_structured_media_from_local_message(
            db,
            msg_hash=msg_hash,
            local_message=local_message,
            public_prefix=media_public_prefix or get_settings().public_storage_prefix,
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
    @staticmethod
    def normalize_message_segment(segment: dict[str, Any]) -> dict[str, Any]:
        raw_type = str(
            segment.get("type") or segment.get("kind") or segment.get("element_type") or "unknown"
        ).strip().lower()
        type_aliases = {
            "quote": "reply",
            "reply_message": "reply",
            "mention": "at",
            "mention_user": "at",
            "text_plain": "text",
        }
        part_type = type_aliases.get(raw_type, raw_type or "unknown")
        raw_data = segment.get("data")
        if isinstance(raw_data, dict):
            data = dict(raw_data)
        else:
            data = {
                key: value
                for key, value in segment.items()
                if key not in {"type", "kind", "element_type"}
            }
        if part_type == "reply":
            reply_id = next(
                (
                    data.get(key)
                    for key in ("id", "message_id", "msg_id", "source_message_id", "msgId")
                    if data.get(key) is not None
                ),
                None,
            )
            if reply_id is not None:
                data["id"] = str(reply_id)
            preview = next(
                (data.get(key) for key in ("preview", "text_preview", "content") if data.get(key)),
                None,
            )
            if preview and "preview" not in data:
                data["preview"] = str(preview)
        elif part_type == "at":
            target_id = next(
                (
                    data.get(key)
                    for key in ("qq", "user_id", "uid", "uin", "target_id", "targetUid")
                    if data.get(key) is not None
                ),
                None,
            )
            if target_id is not None:
                data["qq"] = str(target_id)
            display = next(
                (data.get(key) for key in ("name", "display", "display_name", "text") if data.get(key)),
                None,
            )
            if display and "name" not in data:
                data["name"] = str(display)
        elif part_type == "text" and data.get("text") is None:
            value = data.get("content")
            if value is not None:
                data["text"] = str(value)
        return {"type": part_type[:20], "data": data}
    async def _upsert_message_parts(
        db: AsyncSession,
        *,
        msg_hash: str,
        segments: list[dict[str, Any]] | None,
        source_format: str,
    ) -> None:
        if not isinstance(segments, list):
            return
        for ordinal, segment in enumerate(segments):
            if not isinstance(segment, dict):
                continue
            normalized = MessageService.normalize_message_segment(segment)
            part_type = normalized["type"]
            data = normalized["data"]
            result = await db.execute(
                select(MessagePart).where(MessagePart.msg_hash == msg_hash, MessagePart.ordinal == ordinal).with_for_update()
            )
            part = result.scalar_one_or_none()
            payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
            text_content = str(data.get("text")) if data.get("text") is not None else None
            if part is None:
                db.add(MessagePart(
                    msg_hash=msg_hash, ordinal=ordinal, part_type=part_type,
                    text_content=text_content, payload_json=payload, source_format=source_format,
                    render_status="parsed",
                ))
            else:
                part.part_type = part_type
                part.text_content = text_content
                part.payload_json = payload
                part.source_format = source_format
                part.render_status = "parsed"

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

    @staticmethod
    def _local_message_score(value: str | None) -> tuple[int, int]:
        text = value or ""
        return (text.count("/static/storage/"), len(text))

    @staticmethod
    async def _upgrade_structured_media_from_local_message(
        db: AsyncSession,
        *,
        msg_hash: str,
        local_message: str,
        public_prefix: str,
    ) -> None:
        paths_by_ordinal = MessageService.local_media_paths_by_ordinal(local_message, public_prefix)
        local_paths = [path for path in paths_by_ordinal if path]
        if not local_paths:
            return
        reference_result = await db.execute(
            select(MessageMediaReference)
            .where(MessageMediaReference.msg_hash == msg_hash)
            .order_by(MessageMediaReference.ordinal.asc())
            .with_for_update()
        )
        references = list(reference_result.scalars().all())
        if not references:
            return
        asset_result = await db.execute(select(MediaAsset).where(MediaAsset.local_path.in_(local_paths)))
        asset_by_path = {asset.local_path: asset for asset in asset_result.scalars().all()}
        now = utc_now()
        for reference in references:
            if reference.ordinal >= len(paths_by_ordinal):
                continue
            asset = asset_by_path.get(paths_by_ordinal[reference.ordinal] or "")
            if (
                asset is None
                or asset.file_size <= 0
                or asset.file_type.endswith("_missing")
                or not MessageService.media_types_compatible(asset.file_type, reference.media_type)
            ):
                continue
            if reference.archive_state == "complete":
                continue
            reference.source_state = "downloaded"
            reference.archive_state = "complete"
            reference.asset_file_hash = asset.file_hash
            reference.actual_file_size = asset.file_size
            reference.failure_code = None
            reference.failure_detail = None
            reference.last_checked_at = now
            reference.archived_at = reference.archived_at or now

    @staticmethod
    def local_media_paths_by_ordinal(local_message: str | None, public_prefix: str = "/static/storage") -> list[str | None]:
        from app.services.media_service import _parse_cq_params

        prefixes = tuple(dict.fromkeys((
            public_prefix.rstrip("/") + "/",
            "/media/",
            "/static/storage/",
        )))
        escaped_prefixes = "|".join(re.escape(prefix) for prefix in prefixes)
        local_path_pattern = rf"(?:{escaped_prefixes})[^\s\"'<>),\]]+?\.[a-z0-9]+"
        pattern = re.compile(rf"\[CQ:(?:image|record|video|file),([^\]]*)\]|({local_path_pattern})", re.I)
        paths: list[str | None] = []
        for match in pattern.finditer(local_message or ""):
            if match.group(2):
                paths.append(match.group(2))
                continue
            params = _parse_cq_params(match.group(1) or "")
            candidate = params.get("local") or params.get("file") or params.get("url")
            paths.append(candidate if isinstance(candidate, str) and candidate.startswith(prefixes) else None)
        return paths

    @staticmethod
    def media_types_compatible(asset_type: str, reference_type: str) -> bool:
        normalized_asset = {"audio": "voice", "record": "voice"}.get(asset_type, asset_type)
        normalized_reference = {"audio": "voice", "record": "voice"}.get(reference_type, reference_type)
        return normalized_asset == normalized_reference
