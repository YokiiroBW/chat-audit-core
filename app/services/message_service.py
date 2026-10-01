import hashlib
import json
from pathlib import Path
import re
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.atomic_io import atomic_write_bytes
from app.config import get_settings
from app.storage_paths import count_local_storage_paths, known_storage_prefixes
from app.models import IdentityAlias, MediaAsset, Message, MessageMediaReference, MessagePart, MessageSourceRecord, RobotMessage
from app.services.capture_policy_service import CapturePolicyService
from app.time_utils import utc_now

# Segment types that render an archived file and therefore consume one media
# reference ordinal. Keep in step with the media branch of the web console.
MEDIA_PART_TYPES = frozenset({"image", "record", "video", "file"})


class MessageService:
    @staticmethod
    def generate_md5(content: bytes) -> str:
        return hashlib.md5(content).hexdigest()

    @staticmethod
    def message_hash(
        *,
        platform: str,
        room_id: str,
        sender_id: str,
        event_identity: str,
        message_type: str,
    ) -> str:
        # Preserve the established hash for compatibility with existing rows.
        # A type-qualified fallback is used only when that legacy identity is
        # already occupied by the other kind of conversation.
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
        message_type: str | None = None,
        timestamp: int | None = None,
        raw_message: str | None = None,
    ) -> tuple[str, Message | None]:
        """Find the row this event already lives in, if any.

        The import path carries a stable per-source key, so the same QQNT
        message re-imported twice converges. A realtime NapCat event has only
        its own connection-scoped message id, which never matches the id the
        importer recorded, so archiving the same conversation both ways
        produced two rows for one message -- and only in that order, since an
        import arriving second does find the realtime row through the source
        record lookup below.
        """
        msg_hash = MessageService.message_hash(
            platform=platform,
            room_id=room_id,
            sender_id=sender_id,
            event_identity=event_identity,
            message_type=message_type or "unknown",
        )
        result = await db.execute(select(Message).where(Message.msg_hash == msg_hash).with_for_update())
        message = result.scalar_one_or_none()
        if message is not None and message_type is not None and message.message_type != message_type:
            msg_hash = MessageService.generate_md5(f"{msg_hash}_{message_type}".encode("utf-8"))
            result = await db.execute(select(Message).where(Message.msg_hash == msg_hash).with_for_update())
            message = result.scalar_one_or_none()
        if message is None:
            result = await db.execute(
                select(Message).where(
                    Message.platform == platform,
                    Message.room_id == room_id,
                    Message.sender_id == sender_id,
                    *([Message.message_type == message_type] if message_type is not None else []),
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
                    *([Message.message_type == message_type] if message_type is not None else []),
                    or_(
                        MessageSourceRecord.source_external_message_id == event_identity,
                        MessageSourceRecord.platform_message_id == event_identity,
                    ),
                )
                .with_for_update()
            )
            message = result.scalars().first()
        if message is None and message_type is not None and timestamp is not None and raw_message is not None:
            message = await MessageService._resolve_imported_twin(
                db,
                platform=platform,
                room_id=room_id,
                sender_id=sender_id,
                message_type=message_type,
                timestamp=timestamp,
                raw_message=raw_message,
            )
        return (message.msg_hash if message is not None else msg_hash), message

    @staticmethod
    async def _resolve_imported_twin(
        db: AsyncSession,
        *,
        platform: str,
        room_id: str,
        sender_id: str,
        message_type: str,
        timestamp: int,
        raw_message: str,
    ) -> Message | None:
        """The already-imported row this realtime event is a second copy of.

        Deliberately narrower than the importer's own semantic fallback. It only
        considers rows that carry an import source record, so two realtime
        events can never be merged into each other -- that is the failure this
        avoids repeating, where a group member sending the same short text twice
        in one second lost the second message. An imported row, by contrast,
        cannot be the same realtime delivery arriving twice.
        """
        result = await db.execute(
            select(Message)
            .join(MessageSourceRecord, MessageSourceRecord.msg_hash == Message.msg_hash)
            .where(
                Message.platform == platform,
                Message.room_id == room_id,
                Message.sender_id == sender_id,
                Message.message_type == message_type,
                Message.timestamp == timestamp,
                Message.raw_message == raw_message,
            )
            .order_by(Message.msg_hash.asc())
            .with_for_update()
        )
        return result.scalars().first()

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
            atomic_write_bytes(target_path, file_content)

        local_path = f"{prefix.rstrip('/')}/{filename}"
        asset_values = {
            "file_hash": file_hash,
            "content_sha256": content_sha256,
            "file_type": file_type,
            "file_size": len(file_content),
            "local_path": local_path,
        }
        dialect_name = db.get_bind().dialect.name
        insert_factory = postgresql_insert if dialect_name == "postgresql" else sqlite_insert
        await db.execute(
            insert_factory(MediaAsset)
            .values(**asset_values)
            .on_conflict_do_nothing(index_elements=[MediaAsset.file_hash])
        )
        result = await db.execute(select(MediaAsset).where(MediaAsset.content_sha256 == content_sha256))
        asset = result.scalar_one_or_none()
        if asset is None:
            raise RuntimeError("media asset upsert completed without a readable asset")
        return asset.local_path

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
        # The ingest endpoint accepts canonical ids and the importer keys on
        # them, but this path dropped them, so the same private conversation
        # arriving under two identity variants split into two rooms. Falling
        # back to the raw value keeps callers that send neither unaffected.
        raw_room_id = msg_data["room_id"]
        raw_sender_id = msg_data["sender_id"]
        room_id = str(msg_data.get("canonical_room_id") or raw_room_id)
        sender_id = str(msg_data.get("canonical_sender_id") or raw_sender_id)
        event_identity = msg_data.get("message_id")
        if event_identity is None:
            event_identity = f"{msg_data['timestamp']}_{raw_message}"
        msg_hash, existing_msg = await MessageService.resolve_existing_message(
            db,
            platform=platform,
            room_id=room_id,
            sender_id=sender_id,
            event_identity=str(event_identity),
            message_type=msg_data.get("message_type"),
            timestamp=msg_data.get("timestamp"),
            raw_message=raw_message,
        )

        local_message = msg_data.get("local_message", raw_message)
        message_segments = msg_data.get("message_segments")
        if media_http_client is not None:
            from app.services.media_service import MediaService

            if isinstance(message_segments, list):
                message_segments = await MediaService.localize_onebot_content(
                    db,
                    message_segments,
                    http_client=media_http_client,
                    storage_root=media_storage_root,
                    public_prefix=media_public_prefix,
                    allowed_media_types=capture_decision.allowed_media_types,
                )
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
                    source_sequence=msg_data.get("source_sequence"),
                    is_outgoing=msg_data.get("is_outgoing"),
                )
            )
        else:
            if msg_data.get("is_outgoing") is not None:
                existing_msg.is_outgoing = msg_data.get("is_outgoing")
            if msg_data.get("source_sequence") is not None:
                existing_msg.source_sequence = msg_data.get("source_sequence")
            if msg_data.get("nickname") and (not existing_msg.nickname or msg_data.get("is_outgoing")):
                existing_msg.nickname = msg_data.get("nickname")
            if MessageService._local_message_score(local_message) > MessageService._local_message_score(existing_msg.local_message):
                existing_msg.local_message = local_message

        await MessageService._upsert_message_parts(
            db, msg_hash=msg_hash, segments=message_segments, source_format=msg_data.get("source_event_type") or platform,
        )

        await MessageService._upgrade_structured_media_from_local_message(
            db,
            msg_hash=msg_hash,
            local_message=local_message,
            public_prefix=media_public_prefix or get_settings().public_storage_prefix,
        )
        await MessageService.link_message_parts_to_media(db, msg_hash)

        assoc_result = await db.execute(
            select(RobotMessage).where(
                RobotMessage.robot_id == robot_id,
                RobotMessage.msg_hash == msg_hash,
            )
        )
        if assoc_result.scalar_one_or_none() is None:
            db.add(RobotMessage(robot_id=robot_id, msg_hash=msg_hash))

        # Record how the raw identity maps to the canonical one, the same way
        # the importer does, so the mapping survives for later reconciliation
        # instead of being silently discarded at the door.
        await MessageService._record_identity_alias(
            db, platform=platform, identity_type="user",
            canonical_id=sender_id, alias_id=raw_sender_id, alias_type="realtime_sender_id",
        )
        await MessageService._record_identity_alias(
            db, platform=platform, identity_type="conversation",
            canonical_id=room_id, alias_id=raw_room_id, alias_type="realtime_room_id",
        )

        await db.commit()
        return msg_hash

    @staticmethod
    async def _record_identity_alias(
        db: AsyncSession,
        *,
        platform: str,
        identity_type: str,
        canonical_id: str,
        alias_id: str,
        alias_type: str,
    ) -> None:
        if not alias_id or alias_id == canonical_id:
            return
        result = await db.execute(
            select(IdentityAlias).where(
                IdentityAlias.platform == platform,
                IdentityAlias.identity_type == identity_type,
                IdentityAlias.alias_id == alias_id,
                IdentityAlias.source_id.is_(None),
            ).with_for_update()
        )
        alias = result.scalar_one_or_none()
        if alias is None:
            db.add(IdentityAlias(
                platform=platform, identity_type=identity_type, canonical_id=canonical_id,
                alias_id=alias_id, alias_type=alias_type, source_id=None, confidence="observed",
            ))
            return
        alias.canonical_id = canonical_id
        alias.alias_type = alias_type
        alias.last_seen_at = utc_now()

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
        elif part_type in {"face", "mface", "market_face", "bface"}:
            raw = data.get("raw")
            if isinstance(raw, str):
                try:
                    parsed_raw = json.loads(raw)
                except json.JSONDecodeError:
                    parsed_raw = None
                if isinstance(parsed_raw, dict):
                    data["raw"] = parsed_raw
                    raw = parsed_raw
            if isinstance(raw, dict):
                for key, value in raw.items():
                    data.setdefault(key, value)
            face_id = next(
                (data.get(key) for key in ("id", "face_id", "faceIndex", "face_index", "sticker_id") if data.get(key) is not None),
                None,
            )
            if face_id is not None:
                data["id"] = str(face_id)
            label = next(
                (
                    data.get(key)
                    for key in ("name", "description", "faceText", "face_text", "summary", "text")
                    if data.get(key) and str(data.get(key)) != "[object Object]"
                ),
                None,
            )
            if label is None:
                label = f"QQ 表情 #{data['id']}" if data.get("id") else "QQ 表情"
            data["name"] = str(label)
            for key in ("url", "file", "pack_id", "sticker_id", "face_id"):
                if data.get(key) is not None:
                    data[key] = str(data[key])
        return {"type": part_type[:20], "data": data}

    @staticmethod
    async def link_message_parts_to_media(db: AsyncSession, msg_hash: str) -> int:
        """Point each media part at the media reference it renders.

        Parts are numbered across every segment of a message while media
        references are numbered across media only, so the two ordinals do not line
        up: in "text then image" the image is part ordinal 1 but media ordinal 0.
        A reader pairing them by raw ordinal therefore finds nothing and falls back
        to the remote URL, losing both the archived asset and the availability
        reason that explains why a file is unavailable.

        Resolve the mapping once and store it on the part. Call this after the
        media references exist — parts are written first, so the link cannot be
        made at insert time.
        """
        reference_result = await db.execute(
            select(MessageMediaReference.ordinal, MessageMediaReference.id)
            .where(MessageMediaReference.msg_hash == msg_hash)
        )
        reference_by_ordinal = {ordinal: reference_id for ordinal, reference_id in reference_result.all()}
        part_result = await db.execute(
            select(MessagePart)
            .where(MessagePart.msg_hash == msg_hash)
            .order_by(MessagePart.ordinal.asc())
        )
        linked = 0
        media_ordinal = 0
        for part in part_result.scalars().all():
            is_media_part = str(part.part_type or "").lower() in MEDIA_PART_TYPES
            reference_id = reference_by_ordinal.get(media_ordinal) if is_media_part else None
            if is_media_part:
                media_ordinal += 1
            if part.media_reference_id != reference_id:
                part.media_reference_id = reference_id
                linked += 1
        return linked

    @staticmethod
    async def _upsert_message_parts(
        db: AsyncSession,
        *,
        msg_hash: str,
        segments: list[dict[str, Any]] | None,
        source_format: str,
    ) -> None:
        if not isinstance(segments, list):
            return
        existing_result = await db.execute(
            select(MessagePart)
            .where(MessagePart.msg_hash == msg_hash)
            .order_by(MessagePart.ordinal.asc())
            .with_for_update()
        )
        existing_by_ordinal = {
            part.ordinal: part
            for part in existing_result.scalars().all()
        }
        retained_ordinals: set[int] = set()
        for ordinal, segment in enumerate(segments):
            if not isinstance(segment, dict):
                continue
            retained_ordinals.add(ordinal)
            normalized = MessageService.normalize_message_segment(segment)
            part_type = normalized["type"]
            data = normalized["data"]
            part = existing_by_ordinal.get(ordinal)
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
                # References are positional across media parts. Any authoritative
                # rewrite can change that order, so relink every retained part.
                part.media_reference_id = None

        for ordinal, part in existing_by_ordinal.items():
            if ordinal not in retained_ordinals:
                await db.delete(part)

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
        return (count_local_storage_paths(text), len(text))

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

        prefixes = known_storage_prefixes(public_prefix)
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
