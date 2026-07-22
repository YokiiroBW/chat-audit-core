import base64
import json
import re

from sqlalchemy import and_, case, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models import (
    Adapter,
    BotProfile,
    ImportSource,
    MediaAsset,
    Message,
    MessageMediaReference,
    MessagePart,
    MessageSourceRecord,
    RobotMessage,
    RoomProfile,
    UserProfile,
)


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


def _protobuf_fields(raw: bytes) -> dict[int, list[object]]:
    fields: dict[int, list[object]] = {}
    offset = 0
    while offset < len(raw):
        tag = 0
        shift = 0
        while offset < len(raw):
            byte = raw[offset]
            offset += 1
            tag |= (byte & 0x7F) << shift
            if not byte & 0x80:
                break
            shift += 7
        field_number = tag >> 3
        wire_type = tag & 0x07
        if wire_type == 0:
            value = 0
            shift = 0
            while offset < len(raw):
                byte = raw[offset]
                offset += 1
                value |= (byte & 0x7F) << shift
                if not byte & 0x80:
                    break
                shift += 7
        elif wire_type == 2:
            length = 0
            shift = 0
            while offset < len(raw):
                byte = raw[offset]
                offset += 1
                length |= (byte & 0x7F) << shift
                if not byte & 0x80:
                    break
                shift += 7
            value = raw[offset : offset + length]
            offset += length
        elif wire_type == 1:
            value = raw[offset : offset + 8]
            offset += 8
        elif wire_type == 5:
            value = raw[offset : offset + 4]
            offset += 4
        else:
            break
        fields.setdefault(field_number, []).append(value)
    return fields


def _qqnt_historical_reply_parts(raw_base64: str | None) -> list[dict[str, str]]:
    if not raw_base64:
        return []
    try:
        outer = _protobuf_fields(base64.b64decode(raw_base64))
    except (ValueError, TypeError, base64.binascii.Error):
        return []
    results: list[dict[str, str]] = []
    for content in outer.get(40800, []):
        if not isinstance(content, bytes):
            continue
        fields = _protobuf_fields(content)
        content_type = fields.get(45002, [0])[-1]
        if content_type != 7:
            continue
        reply_id = fields.get(47422, [0])[-1]
        if not reply_id:
            continue
        preview = ""
        for nested in fields.get(47423, []):
            if not isinstance(nested, bytes):
                continue
            nested_fields = _protobuf_fields(nested)
            text_values = nested_fields.get(45101, [])
            for value in text_values:
                if isinstance(value, bytes):
                    try:
                        preview = value.decode("utf-8")
                    except UnicodeDecodeError:
                        preview = ""
                    if preview:
                        break
            if not preview and nested_fields.get(45402):
                preview = "[鍥剧墖]"
            if preview:
                break
        results.append({"id": str(reply_id), "preview": preview})
    return results

def _message_reply_id(message: Message) -> str | None:
    reply_id = _parse_reply_id(message.local_message or message.raw_message)
    if reply_id:
        return reply_id
    for part in getattr(message, "parts", []) or []:
        if str(part.part_type or "").lower() != "reply":
            continue
        try:
            payload = json.loads(part.payload_json or "{}")
        except (TypeError, json.JSONDecodeError):
            payload = {}
        for key in ("id", "message_id", "msg_id", "source_message_id", "msgId"):
            if payload.get(key) is not None:
                return str(payload[key])
    return None

def _plain_message_preview(value: str | None) -> str:
    text = value or ""
    text = re.sub(r"\[CQ:reply,[^\]]+\]", "", text)
    text = re.sub(r"\[CQ:at,qq=([^\],]+)[^\]]*\]", r"@\1", text)
    text = re.sub(r"\[CQ:image,[^\]]+\]", "[鍥剧墖]", text)
    text = re.sub(r"\[CQ:record,[^\]]+\]", "[璇煶]", text)
    text = re.sub(r"\[CQ:video,[^\]]+\]", "[瑙嗛]", text)
    text = re.sub(r"\[CQ:forward,[^\]]+\]", "[鍚堝苟杞彂]", text)
    text = re.sub(r"\[CQ:json,[^\]]+\]", "[鍗＄墖]", text)
    text = re.sub(r"/static/storage/[a-f0-9]{32}\.[a-z0-9]+", "[濯掍綋]", text, flags=re.I)
    compact = re.sub(r"\s+", " ", text).strip()
    return compact or "[娑堟伅]"


def _prefer_avatar_path(*paths: str | None) -> str | None:
    values = [str(path).strip() for path in paths if path]
    for value in values:
        if value.startswith("/static/storage/"):
            return value
    return values[0] if values else None


def _usable_profile_name(
    value: str | None,
    *,
    robot_id: str,
    robot_display_name: str | None,
    profile_id: str | None = None,
) -> str | None:
    name = str(value or "").strip()
    if not name:
        return None
    if (
        profile_id
        and str(profile_id) != str(robot_id)
        and robot_display_name
        and name.casefold() == str(robot_display_name).strip().casefold()
    ):
        return None
    return name


async def _private_robot_display_name(
    db: AsyncSession,
    robot_id: str,
    room_id: str | None = None,
) -> str | None:
    stmt = (
        select(Message.nickname)
        .join(RobotMessage, RobotMessage.msg_hash == Message.msg_hash)
        .where(
            RobotMessage.robot_id == robot_id,
            Message.message_type == "private",
            Message.sender_id != Message.room_id,
            Message.nickname.is_not(None),
        )
        .order_by(Message.timestamp.desc())
        .limit(1)
    )
    if room_id:
        stmt = stmt.where(Message.room_id == room_id)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


def _qq_number_from_raw(
    raw_columns_json: str | None,
    robot_id: str,
    *,
    message_type: str | None = None,
    exclude_robot: bool = True,
) -> str | None:
    try:
        raw = json.loads(raw_columns_json or "{}")
    except (TypeError, json.JSONDecodeError):
        return None
    if message_type == "private":
        sender_identity = str(raw.get("40020") or "").strip()
        conversation_identity = str(raw.get("40021") or "").strip()
        if sender_identity and conversation_identity and sender_identity != conversation_identity:
            return str(robot_id) if not exclude_robot else None
        private_peer = str(raw.get("40030") or "").strip()
        if private_peer.isdigit() and 5 <= len(private_peer) <= 12:
            return private_peer
    for key in ("qq", "uin", "user_id", "40033", "40030"):
        value = str(raw.get(key) or "").strip()
        if (
            value.isdigit()
            and (not exclude_robot or value != str(robot_id))
            and 5 <= len(value) <= 12
        ):
            return value
    return None


class QueryService:
    @staticmethod
    async def list_adapters(db: AsyncSession) -> list[Adapter]:
        result = await db.execute(select(Adapter).order_by(Adapter.id.asc()))
        return list(result.scalars().all())

    @staticmethod
    async def list_bot_profiles(db: AsyncSession) -> list[BotProfile]:
        result = await db.execute(
            select(BotProfile, UserProfile.avatar_path.label("avatar_path"))
            .outerjoin(UserProfile, UserProfile.user_id == BotProfile.id)
            .order_by(BotProfile.last_seen_at.desc(), BotProfile.id.asc())
        )
        profiles = []
        for profile, avatar_path in result.all():
            profile.avatar_path = avatar_path
            profiles.append(profile)
        return profiles

    @staticmethod
    async def list_rooms(db: AsyncSession, robot_id: str) -> list[dict]:
        bot_result = await db.execute(
            select(BotProfile.display_name).where(BotProfile.id == robot_id)
        )
        robot_display_name = bot_result.scalar_one_or_none() or await _private_robot_display_name(db, robot_id)
        private_sender_display_name = case(
            ((Message.message_type == "private") & (Message.sender_id == Message.room_id), Message.nickname),
            else_=None,
        )
        result = await db.execute(
            select(
                Message.room_id.label("room_id"),
                func.max(Message.timestamp).label("last_timestamp"),
                func.max(Message.message_type).label("message_type"),
                func.max(func.coalesce(RoomProfile.display_name, UserProfile.display_name, private_sender_display_name)).label("display_name"),
                func.max(func.coalesce(RoomProfile.avatar_path, UserProfile.avatar_path)).label("avatar_path"),
            )
            .join(RobotMessage, RobotMessage.msg_hash == Message.msg_hash)
            .outerjoin(RoomProfile, RoomProfile.room_id == Message.room_id)
            .outerjoin(UserProfile, UserProfile.user_id == Message.room_id)
            .where(RobotMessage.robot_id == robot_id)
            .group_by(Message.room_id)
            .order_by(desc("last_timestamp"), Message.room_id.asc())
        )
        rows = result.all()
        room_ids = [row.room_id for row in rows]
        qq_candidates: dict[str, list[str]] = {room_id: [] for room_id in room_ids}
        if room_ids:
            ranked_sources = (
                select(
                    Message.room_id.label("room_id"),
                    Message.message_type.label("message_type"),
                    MessageSourceRecord.raw_columns_json.label("raw_columns_json"),
                    func.row_number().over(
                        partition_by=Message.room_id,
                        order_by=(Message.timestamp.desc(), func.coalesce(Message.source_sequence, -1).desc(), Message.msg_hash.desc()),
                    ).label("room_rank"),
                )
                .join(RobotMessage, RobotMessage.msg_hash == Message.msg_hash)
                .outerjoin(MessageSourceRecord, MessageSourceRecord.msg_hash == Message.msg_hash)
                .where(RobotMessage.robot_id == robot_id, Message.room_id.in_(room_ids), Message.message_type == "private")
                .subquery()
            )
            source_result = await db.execute(
                select(ranked_sources.c.room_id, ranked_sources.c.message_type, ranked_sources.c.raw_columns_json)
                .where(ranked_sources.c.room_rank <= 50)
            )
            for room_id, message_type, raw_columns_json in source_result.all():
                qq_number = _qq_number_from_raw(raw_columns_json, robot_id, message_type=message_type)
                if qq_number:
                    qq_candidates[room_id].append(qq_number)

        profile_ids = {
            qq_number
            for candidates in qq_candidates.values()
            for qq_number in candidates
        }
        profile_result = await db.execute(
            select(UserProfile.user_id, UserProfile.display_name, UserProfile.avatar_path)
            .where(UserProfile.user_id.in_(profile_ids))
        ) if profile_ids else None
        profiles = {
            row.user_id: row
            for row in (profile_result.all() if profile_result is not None else [])
        }

        room_items = []
        for row in rows:
            candidates = qq_candidates.get(row.room_id, [])
            qq_number = max(set(candidates), key=candidates.count) if candidates else None
            if not qq_number and row.message_type == "private" and str(row.room_id).isdigit():
                qq_number = str(row.room_id)
            profile = profiles.get(qq_number)
            if row.message_type == "private":
                display_name = _usable_profile_name(
                    profile.display_name if profile else None,
                    robot_id=robot_id,
                    robot_display_name=robot_display_name,
                    profile_id=qq_number,
                )
                if display_name is None:
                    display_name = _usable_profile_name(
                        row.display_name,
                        robot_id=robot_id,
                        robot_display_name=robot_display_name,
                        profile_id=qq_number,
                    )
            else:
                display_name = row.display_name or (profile.display_name if profile else None)
            room_items.append({
                "room_id": row.room_id,
                "last_timestamp": row.last_timestamp,
                "message_type": row.message_type,
                "display_name": display_name,
                "avatar_path": _prefer_avatar_path(profile.avatar_path if profile else None, row.avatar_path),
                "qq_number": qq_number,
            })
        return room_items

    @staticmethod
    async def list_messages(
        db: AsyncSession,
        robot_id: str,
        room_id: str,
        before_timestamp: int | None = None,
        before_source_sequence: int | None = None,
        before_msg_hash: str | None = None,
        around_message_id: str | None = None,
        media_state: str | None = None,
        limit: int = 50,
    ) -> list[Message]:
        bot_result = await db.execute(
            select(BotProfile.display_name).where(BotProfile.id == robot_id)
        )
        robot_display_name = bot_result.scalar_one_or_none() or await _private_robot_display_name(db, robot_id, room_id)
        if around_message_id:
            target_result = await db.execute(
                select(Message)
                .join(RobotMessage, RobotMessage.msg_hash == Message.msg_hash)
                .where(
                    RobotMessage.robot_id == robot_id,
                    Message.room_id == room_id,
                    Message.external_message_id == around_message_id,
                )
                .limit(1)
            )
            target = target_result.scalars().first()
            if target is None:
                alias_result = await db.execute(
                    select(Message)
                    .join(RobotMessage, RobotMessage.msg_hash == Message.msg_hash)
                    .join(MessageSourceRecord, MessageSourceRecord.msg_hash == Message.msg_hash)
                    .where(
                        RobotMessage.robot_id == robot_id,
                        Message.room_id == room_id,
                        or_(
                            MessageSourceRecord.source_external_message_id == around_message_id,
                            MessageSourceRecord.platform_message_id == around_message_id,
                        ),
                    )
                    .limit(1)
                )
                target = alias_result.scalars().first()
            if target is None:
                return []
            before_timestamp = target.timestamp + 1

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
        stmt = QueryService._apply_media_state_filter(stmt, media_state)
        if before_timestamp is not None:
            if before_source_sequence is not None and before_msg_hash:
                source_sequence = func.coalesce(Message.source_sequence, -1)
                stmt = stmt.where(
                    or_(
                        Message.timestamp < before_timestamp,
                        and_(
                            Message.timestamp == before_timestamp,
                            or_(
                                source_sequence < before_source_sequence,
                                and_(source_sequence == before_source_sequence, Message.msg_hash < before_msg_hash),
                            ),
                        ),
                    )
                )
            else:
                stmt = stmt.where(Message.timestamp < before_timestamp)

        # Cursor loading needs the newest N messages before the cursor, then returns
        # them in chronological order for stable chat rendering.
        stmt = stmt.order_by(
            Message.timestamp.desc(),
            func.coalesce(Message.source_sequence, -1).desc(),
            Message.msg_hash.desc(),
        ).limit(limit)
        result = await db.execute(stmt)
        messages = []
        for message, sender_display_name, sender_avatar_path in result.all():
            message.sender_display_name = sender_display_name
            message.sender_avatar_path = sender_avatar_path
            messages.append(message)
        messages = list(reversed(messages))
        message_types = {message.msg_hash: message.message_type for message in messages}
        source_result = await db.execute(
            select(MessageSourceRecord.msg_hash, MessageSourceRecord.raw_columns_json)
            .where(MessageSourceRecord.msg_hash.in_([message.msg_hash for message in messages]))
        )
        outgoing_by_hash: dict[str, bool] = {}
        profile_id_by_hash: dict[str, str] = {}
        for msg_hash, raw_columns_json in source_result.all():
            try:
                raw_columns = json.loads(raw_columns_json or "{}")
            except (TypeError, json.JSONDecodeError):
                raw_columns = {}
            message_type = message_types.get(msg_hash)
            if message_type == "private":
                sender_identity = str(raw_columns.get("40020") or "").strip()
                conversation_identity = str(raw_columns.get("40021") or "").strip()
                if sender_identity and conversation_identity:
                    outgoing_by_hash[msg_hash] = sender_identity != conversation_identity
            elif "40033" in raw_columns:
                outgoing_by_hash[msg_hash] = outgoing_by_hash.get(msg_hash, False) or str(raw_columns.get("40033")) == str(robot_id)

            profile_id = _qq_number_from_raw(
                raw_columns_json,
                robot_id,
                message_type=message_types.get(msg_hash),
                exclude_robot=False,
            )
            if profile_id:
                profile_id_by_hash[msg_hash] = profile_id

        profile_ids = set(profile_id_by_hash.values())
        profile_result = await db.execute(
            select(UserProfile.user_id, UserProfile.display_name, UserProfile.avatar_path)
            .where(UserProfile.user_id.in_(profile_ids))
        ) if profile_ids else None
        profiles = {
            row.user_id: row
            for row in (profile_result.all() if profile_result is not None else [])
        }
        for message in messages:
            message.is_outgoing = outgoing_by_hash.get(message.msg_hash)
            profile_id = profile_id_by_hash.get(message.msg_hash)
            profile = profiles.get(profile_id)
            message.sender_qq_number = profile_id
            if message.message_type == "private":
                candidates = [
                    message.nickname,
                    message.sender_display_name,
                    profile.display_name if profile else None,
                ]
                message.sender_display_name = next(
                    (
                        name
                        for name in (
                            _usable_profile_name(
                                candidate,
                                robot_id=robot_id,
                                robot_display_name=robot_display_name,
                                profile_id=profile_id,
                            )
                            for candidate in candidates
                        )
                        if name
                    ),
                    None,
                )
            elif profile:
                message.sender_display_name = message.sender_display_name or profile.display_name
            if profile:
                message.sender_avatar_path = _prefer_avatar_path(profile.avatar_path, message.sender_avatar_path)
        await QueryService._attach_parts(db, messages)
        await QueryService._attach_reply_previews(db, messages)
        await QueryService._attach_import_details(db, messages)
        return messages

    @staticmethod
    async def _attach_parts(db: AsyncSession, messages: list[Message]) -> None:
        if not messages:
            return
        message_hashes = [message.msg_hash for message in messages]
        result = await db.execute(
            select(MessagePart)
            .where(MessagePart.msg_hash.in_(message_hashes))
            .order_by(MessagePart.msg_hash.asc(), MessagePart.ordinal.asc())
        )
        parts_by_hash: dict[str, list[MessagePart]] = {}
        for part in result.scalars().all():
            parts_by_hash.setdefault(part.msg_hash, []).append(part)
        source_result = await db.execute(
            select(MessageSourceRecord.msg_hash, MessageSourceRecord.raw_40800_protobuf)
            .where(MessageSourceRecord.msg_hash.in_(message_hashes))
        )
        raw_by_hash = {msg_hash: raw for msg_hash, raw in source_result.all()}
        for message in messages:
            parts = parts_by_hash.setdefault(message.msg_hash, [])
            historical_replies = _qqnt_historical_reply_parts(raw_by_hash.get(message.msg_hash))
            for reply_index, reply in enumerate(historical_replies):
                part = next((item for item in parts if item.ordinal == reply_index), None)
                payload = json.dumps(reply, ensure_ascii=False, sort_keys=True)
                if part is None:
                    part = MessagePart(
                        id=-(reply_index + 1), msg_hash=message.msg_hash, ordinal=reply_index,
                        part_type="reply", text_content=None, payload_json=payload,
                        source_format="qqnt_local_db", render_status="parsed",
                    )
                    parts.append(part)
                elif part.part_type in {"system", "unknown", "reply"}:
                    part.part_type = "reply"
                    part.text_content = None
                    part.payload_json = payload
                    part.source_format = part.source_format or "qqnt_local_db"
                    part.render_status = "parsed"
            message.parts = sorted(parts, key=lambda item: item.ordinal)
    @staticmethod
    async def _attach_reply_previews(db: AsyncSession, messages: list[Message]) -> None:
        reply_ids = {
            reply_id
            for message in messages
            if (reply_id := _message_reply_id(message))
        }
        if not reply_ids:
            return
        result = await db.execute(select(Message).where(Message.external_message_id.in_(reply_ids)))
        by_external_id = {message.external_message_id: message for message in result.scalars().all() if message.external_message_id}
        unresolved_ids = reply_ids - set(by_external_id)
        if unresolved_ids:
            alias_result = await db.execute(
                select(MessageSourceRecord, Message)
                .join(Message, Message.msg_hash == MessageSourceRecord.msg_hash)
                .where(
                    or_(
                        MessageSourceRecord.source_external_message_id.in_(unresolved_ids),
                        MessageSourceRecord.platform_message_id.in_(unresolved_ids),
                    )
                )
            )
            for record, source_message in alias_result.all():
                for alias in (record.source_external_message_id, record.platform_message_id):
                    if alias in unresolved_ids:
                        by_external_id[alias] = source_message
        for message in messages:
            reply_id = _message_reply_id(message)
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
        media_state: str | None = None,
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
        stmt = QueryService._apply_media_state_filter(stmt, media_state)

        stmt = stmt.order_by(
            Message.timestamp.desc(),
            func.coalesce(Message.source_sequence, -1).desc(),
            Message.msg_hash.desc(),
        ).limit(limit)
        result = await db.execute(stmt)
        messages = list(result.scalars().unique().all())
        await QueryService._attach_reply_previews(db, messages)
        await QueryService._attach_import_details(db, messages)
        return messages

    @staticmethod
    def _apply_media_state_filter(stmt, media_state: str | None):
        if not media_state or media_state == "all":
            return stmt
        media_stmt = select(MessageMediaReference.msg_hash)
        if media_state == "any":
            pass
        elif media_state == "complete":
            media_stmt = media_stmt.where(MessageMediaReference.archive_state == "complete")
        elif media_state == "not_downloaded":
            media_stmt = media_stmt.where(MessageMediaReference.source_state == "not_downloaded")
        elif media_state == "missing":
            media_stmt = media_stmt.where(MessageMediaReference.source_state == "missing")
        elif media_state == "failed":
            media_stmt = media_stmt.where(MessageMediaReference.archive_state == "failed")
        else:
            raise ValueError(f"unsupported media_state: {media_state}")
        return stmt.where(Message.msg_hash.in_(media_stmt))

    @staticmethod
    async def _attach_import_details(db: AsyncSession, messages: list[Message]) -> None:
        if not messages:
            return
        msg_hashes = [message.msg_hash for message in messages]
        by_hash = {message.msg_hash: message for message in messages}
        for message in messages:
            message.media = []
            message.import_sources = []
            message.external_message_aliases = []

        asset_model = aliased(MediaAsset)
        thumbnail_model = aliased(MediaAsset)
        media_result = await db.execute(
            select(
                MessageMediaReference,
                asset_model.local_path.label("asset_local_path"),
                thumbnail_model.local_path.label("thumbnail_local_path"),
            )
            .outerjoin(asset_model, asset_model.file_hash == MessageMediaReference.asset_file_hash)
            .outerjoin(thumbnail_model, thumbnail_model.file_hash == MessageMediaReference.thumbnail_file_hash)
            .where(MessageMediaReference.msg_hash.in_(msg_hashes))
            .order_by(MessageMediaReference.msg_hash.asc(), MessageMediaReference.ordinal.asc())
        )
        for reference, asset_local_path, thumbnail_local_path in media_result.all():
            reference.asset_local_path = asset_local_path
            reference.thumbnail_local_path = thumbnail_local_path
            try:
                metadata = json.loads(reference.metadata_json) if reference.metadata_json else {}
            except json.JSONDecodeError:
                metadata = {}
            reference.metadata = metadata if isinstance(metadata, dict) else {}
            by_hash[reference.msg_hash].media.append(reference)

        source_result = await db.execute(
            select(MessageSourceRecord, ImportSource)
            .join(ImportSource, ImportSource.id == MessageSourceRecord.source_id)
            .where(MessageSourceRecord.msg_hash.in_(msg_hashes))
            .order_by(MessageSourceRecord.msg_hash.asc(), MessageSourceRecord.imported_at.asc())
        )
        source_by_message: dict[str, dict[str, dict]] = {msg_hash: {} for msg_hash in msg_hashes}
        aliases_by_message: dict[str, set[str]] = {msg_hash: set() for msg_hash in msg_hashes}
        for record, source in source_result.all():
            aliases_by_message[record.msg_hash].update(
                alias
                for alias in (record.source_external_message_id, record.platform_message_id)
                if alias
            )
            existing = source_by_message[record.msg_hash].get(source.id)
            summary = {
                "source_id": source.id,
                "source_type": source.source_type,
                "device_name": source.device_name,
                "qq_version": source.qq_version,
                "schema_version": record.schema_version or source.schema_version,
                "imported_at": record.imported_at,
                "last_seen_at": record.last_seen_at,
            }
            if existing is None:
                source_by_message[record.msg_hash][source.id] = summary
            else:
                existing["imported_at"] = min(existing["imported_at"], record.imported_at)
                existing["last_seen_at"] = max(existing["last_seen_at"], record.last_seen_at)
        for msg_hash, sources in source_by_message.items():
            by_hash[msg_hash].import_sources = list(sources.values())
            by_hash[msg_hash].external_message_aliases = sorted(aliases_by_message[msg_hash])
