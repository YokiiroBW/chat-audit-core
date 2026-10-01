import base64
import json
import re

from sqlalchemy import and_, case, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.orm.attributes import set_committed_value

from app.conversation_identity import resolve_conversation_message_type
from app.message_scope import (
    apply_robot_message_scope,
    robot_message_join,
    robot_message_scope,
)
from app.qqnt_identity import (
    GROUP_SENDER_QQ_COLUMN,
    PRIVATE_PEER_QQ_COLUMN,
    is_qq_number,
    load_raw_columns,
    message_is_outgoing,
    private_is_outgoing,
    private_peer_qq,
)
from app.storage_paths import is_local_storage_path, stored_media_pattern

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
                preview = "[图片]"
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
    text = re.sub(r"\[CQ:image,[^\]]+\]", "[图片]", text)
    text = re.sub(r"\[CQ:record,[^\]]+\]", "[语音]", text)
    text = re.sub(r"\[CQ:video,[^\]]+\]", "[视频]", text)
    text = re.sub(r"\[CQ:forward,[^\]]+\]", "[合并转发]", text)
    text = re.sub(r"\[CQ:json,[^\]]+\]", "[卡片]", text)
    text = stored_media_pattern().sub("[媒体]", text)
    compact = re.sub(r"\s+", " ", text).strip()
    return compact or "[消息]"


def _prefer_avatar_path(*paths: str | None) -> str | None:
    values = [str(path).strip() for path in paths if path]
    for value in values:
        if is_local_storage_path(value):
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
        .outerjoin(RobotMessage, robot_message_join(robot_id))
        .where(
            robot_message_scope(robot_id),
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
    raw = load_raw_columns(raw_columns_json)
    if message_type == "private":
        if private_is_outgoing(raw):
            # The account itself sent this one, so the peer column is not the sender.
            return None if exclude_robot else str(robot_id)
        peer = private_peer_qq(raw)
        if peer:
            return peer
    for key in ("qq", "uin", "user_id", GROUP_SENDER_QQ_COLUMN, PRIVATE_PEER_QQ_COLUMN):
        value = str(raw.get(key) or "").strip()
        if is_qq_number(value) and (not exclude_robot or value != str(robot_id)):
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
                Message.message_type.label("message_type"),
                func.max(func.coalesce(RoomProfile.display_name, UserProfile.display_name, private_sender_display_name)).label("display_name"),
                func.max(func.coalesce(RoomProfile.avatar_path, UserProfile.avatar_path)).label("avatar_path"),
            )
            .outerjoin(RobotMessage, robot_message_join(robot_id))
            .outerjoin(
                RoomProfile,
                and_(Message.message_type == "group", RoomProfile.room_id == Message.room_id),
            )
            .outerjoin(
                UserProfile,
                and_(Message.message_type == "private", UserProfile.user_id == Message.room_id),
            )
            .where(robot_message_scope(robot_id))
            .group_by(Message.room_id, Message.message_type)
            .order_by(desc("last_timestamp"), Message.room_id.asc(), Message.message_type.asc())
        )
        rows = result.all()
        room_ids = sorted({row.room_id for row in rows})
        qq_candidates: dict[tuple[str, str], list[str]] = {
            (row.room_id, row.message_type): [] for row in rows
        }
        if room_ids:
            ranked_sources = (
                select(
                    Message.room_id.label("room_id"),
                    Message.message_type.label("message_type"),
                    MessageSourceRecord.raw_columns_json.label("raw_columns_json"),
                    func.row_number().over(
                        partition_by=(Message.room_id, Message.message_type),
                        order_by=(Message.timestamp.desc(), func.coalesce(Message.source_sequence, -1).desc(), Message.msg_hash.desc()),
                    ).label("room_rank"),
                )
                .outerjoin(RobotMessage, robot_message_join(robot_id))
                .outerjoin(MessageSourceRecord, MessageSourceRecord.msg_hash == Message.msg_hash)
                .where(robot_message_scope(robot_id), Message.room_id.in_(room_ids), Message.message_type == "private")
                .subquery()
            )
            source_result = await db.execute(
                select(ranked_sources.c.room_id, ranked_sources.c.message_type, ranked_sources.c.raw_columns_json)
                .where(ranked_sources.c.room_rank <= 50)
            )
            for room_id, message_type, raw_columns_json in source_result.all():
                qq_number = _qq_number_from_raw(raw_columns_json, robot_id, message_type=message_type)
                if qq_number:
                    qq_candidates[(room_id, message_type)].append(qq_number)

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
            candidates = qq_candidates.get((row.room_id, row.message_type), [])
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
        message_type: str | None = None,
        before_timestamp: int | None = None,
        before_source_sequence: int | None = None,
        before_msg_hash: str | None = None,
        around_message_id: str | None = None,
        media_state: str | None = None,
        limit: int = 50,
    ) -> list[Message]:
        message_type = await resolve_conversation_message_type(
            db,
            robot_id=robot_id,
            room_id=room_id,
            message_type=message_type,
        )
        bot_result = await db.execute(
            select(BotProfile.display_name).where(BotProfile.id == robot_id)
        )
        robot_display_name = bot_result.scalar_one_or_none() or await _private_robot_display_name(db, robot_id, room_id)
        if around_message_id:
            target_result = await db.execute(
                select(Message)
                .outerjoin(RobotMessage, robot_message_join(robot_id))
                .where(
                    robot_message_scope(robot_id),
                    Message.room_id == room_id,
                    *([Message.message_type == message_type] if message_type is not None else []),
                    Message.external_message_id == around_message_id,
                )
                .limit(1)
            )
            target = target_result.scalars().first()
            if target is None:
                alias_result = await db.execute(
                    select(Message)
                    .outerjoin(RobotMessage, robot_message_join(robot_id))
                    .join(MessageSourceRecord, MessageSourceRecord.msg_hash == Message.msg_hash)
                    .where(
                        robot_message_scope(robot_id),
                        Message.room_id == room_id,
                        *([Message.message_type == message_type] if message_type is not None else []),
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
            before_timestamp = target.timestamp
            before_source_sequence = target.source_sequence if target.source_sequence is not None else -1
            before_msg_hash = target.msg_hash

        stmt = (
            select(
                Message,
                UserProfile.display_name.label("sender_display_name"),
                UserProfile.avatar_path.label("sender_avatar_path"),
            )
            .outerjoin(RobotMessage, robot_message_join(robot_id))
            .outerjoin(UserProfile, UserProfile.user_id == Message.sender_id)
            .where(robot_message_scope(robot_id), Message.room_id == room_id)
        )
        if message_type is not None:
            stmt = stmt.where(Message.message_type == message_type)
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
                                and_(
                                    source_sequence == before_source_sequence,
                                    Message.msg_hash <= before_msg_hash if around_message_id else Message.msg_hash < before_msg_hash,
                                ),
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
            raw_columns = load_raw_columns(raw_columns_json)
            message_type = message_types.get(msg_hash)
            outgoing = message_is_outgoing(
                raw_columns, message_type=message_type, account_id=robot_id
            )
            if outgoing is not None:
                if message_type == "private":
                    outgoing_by_hash[msg_hash] = outgoing
                else:
                    # Several sources may describe one group message; any source
                    # that attributes it to this account settles the direction.
                    outgoing_by_hash[msg_hash] = outgoing_by_hash.get(msg_hash, False) or outgoing

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
            # Only messages carrying a source record have a derivable direction.
            # Assigning ``None`` for the rest would erase the value the realtime
            # ingest path stored, and because this object stays attached to the
            # request session, any later commit (avatar hydration, for one) would
            # persist that erasure. ``set_committed_value`` attaches the derived
            # value for serialisation without marking the row dirty.
            if message.msg_hash in outgoing_by_hash:
                set_committed_value(message, "is_outgoing", outgoing_by_hash[message.msg_hash])
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
        await QueryService._attach_reply_previews(db, robot_id, messages)
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
                part_index = next(
                    (index for index, item in enumerate(parts) if item.ordinal == reply_index),
                    None,
                )
                payload = json.dumps(reply, ensure_ascii=False, sort_keys=True)
                if part_index is None:
                    parts.append(MessagePart(
                        id=-(reply_index + 1), msg_hash=message.msg_hash, ordinal=reply_index,
                        part_type="reply", text_content=None, payload_json=payload,
                        source_format="qqnt_local_db", render_status="parsed",
                    ))
                    continue
                part = parts[part_index]
                if part.part_type in {"system", "unknown", "reply"}:
                    # Render-only overlay. Mutating the loaded row here would turn a
                    # read into a pending UPDATE that the next commit in this session
                    # writes back, so swap in a detached copy instead.
                    parts[part_index] = MessagePart(
                        id=part.id, msg_hash=part.msg_hash, ordinal=part.ordinal,
                        part_type="reply", text_content=None,
                        media_reference_id=part.media_reference_id, payload_json=payload,
                        source_format=part.source_format or "qqnt_local_db",
                        render_status="parsed",
                    )
            message.parts = sorted(parts, key=lambda item: item.ordinal)


    @staticmethod
    async def _attach_reply_previews(db: AsyncSession, robot_id: str, messages: list[Message]) -> None:
        """Fill in ``reply_to_message_id`` / ``reply_preview_text`` for ``messages``.

        The lookup is keyed on ``(room_id, external_message_id)`` and restricted
        to what ``robot_id`` may see, because neither half alone is enough.
        ``external_message_id`` carries the OneBot message id, which is a small
        per-connection integer with no uniqueness guarantee (``models.py`` only
        indexes it): the same id routinely denotes different messages in
        different accounts and different rooms. Resolving it globally therefore
        rendered a foreign message's sender and body as the preview -- a wrong
        preview at best, verbatim content from a conversation the caller cannot
        open at worst. A reply always targets a message in its own room, so the
        room is part of the identity, not an optional narrowing.
        """
        # Track (room, reply id) pairs rather than bare ids: the same id can be
        # directly resolvable in one room of a cross-room search page while only
        # the alias fallback can resolve it in another, and collapsing to ids
        # would drop the second room's preview.
        wanted = {
            (message.room_id, message.message_type, reply_id)
            for message in messages
            if (reply_id := _message_reply_id(message))
        }
        if not wanted:
            return
        result = await db.execute(
            apply_robot_message_scope(select(Message), robot_id)
            .where(
                Message.external_message_id.in_({reply_id for _, _, reply_id in wanted}),
                Message.room_id.in_({room_id for room_id, _, _ in wanted}),
                Message.message_type.in_({message_type for _, message_type, _ in wanted}),
            )
            # Newest first, so the deterministic winner below is the most recent
            # message carrying a reused id within the same room.
            .order_by(Message.timestamp.desc(), Message.msg_hash.desc())
        )
        by_room_and_id: dict[tuple[str, str, str], Message] = {}
        for candidate in result.scalars().all():
            if candidate.external_message_id:
                by_room_and_id.setdefault(
                    (candidate.room_id, candidate.message_type, candidate.external_message_id), candidate
                )
        unresolved = wanted - set(by_room_and_id)
        if unresolved:
            unresolved_ids = {reply_id for _, _, reply_id in unresolved}
            alias_result = await db.execute(
                apply_robot_message_scope(
                    select(MessageSourceRecord, Message).join(Message, Message.msg_hash == MessageSourceRecord.msg_hash),
                    robot_id,
                )
                .where(
                    or_(
                        MessageSourceRecord.source_external_message_id.in_(unresolved_ids),
                        MessageSourceRecord.platform_message_id.in_(unresolved_ids),
                    ),
                    Message.room_id.in_({room_id for room_id, _, _ in unresolved}),
                    Message.message_type.in_({message_type for _, message_type, _ in unresolved}),
                )
                .order_by(Message.timestamp.desc(), Message.msg_hash.desc())
            )
            for record, source_message in alias_result.all():
                for alias in (record.source_external_message_id, record.platform_message_id):
                    key = (source_message.room_id, source_message.message_type, alias)
                    if key in unresolved:
                        by_room_and_id.setdefault(key, source_message)
        for message in messages:
            reply_id = _message_reply_id(message)
            message.reply_to_message_id = reply_id
            source = by_room_and_id.get((message.room_id, message.message_type, reply_id)) if reply_id else None
            if source is not None:
                sender = source.nickname or source.sender_id
                message.reply_preview_text = f"{sender}: {_plain_message_preview(source.local_message or source.raw_message)}"

    @staticmethod
    async def search_messages(
        db: AsyncSession,
        robot_id: str,
        keyword: str | None = None,
        room_id: str | None = None,
        message_type: str | None = None,
        sender_id: str | None = None,
        start_timestamp: int | None = None,
        end_timestamp: int | None = None,
        media_state: str | None = None,
        limit: int = 50,
    ) -> list[Message]:
        if room_id:
            message_type = await resolve_conversation_message_type(
                db,
                robot_id=robot_id,
                room_id=room_id,
                message_type=message_type,
            )
        stmt = (
            select(Message)
            .outerjoin(RobotMessage, robot_message_join(robot_id))
            .where(robot_message_scope(robot_id))
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
        if message_type:
            stmt = stmt.where(Message.message_type == message_type)
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
        await QueryService._attach_reply_previews(db, robot_id, messages)
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
            message = by_hash[msg_hash]
            message.import_sources = list(sources.values())
            message.external_message_aliases = sorted(aliases_by_message[msg_hash])
            source_types = {source["source_type"] for source in sources.values()}
            for reference in message.media:
                if reference.source_state == "not_downloaded":
                    reference.availability_reason = "source_not_downloaded"
                elif reference.source_state == "missing":
                    reference.availability_reason = "source_missing"
                elif reference.archive_state == "failed":
                    reference.availability_reason = (
                        "archive_failed" if "qqnt_local_db" in source_types else "napcat_download_failed"
                    )
                elif reference.archive_state == "thumbnail_only":
                    reference.availability_reason = "thumbnail_only"
                elif reference.archive_state == "complete" and not reference.asset_local_path:
                    reference.availability_reason = "asset_unavailable"
                else:
                    reference.availability_reason = None
