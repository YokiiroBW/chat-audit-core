from __future__ import annotations

import base64
import copy
import hashlib
from dataclasses import dataclass
from typing import Any, Mapping

from collector.media.resolver import MediaResolver
from collector.media.staging import MediaStagingStore, StagingError
from collector.parsers.base import GeneratedArtifact, ParsedMediaUpload, ParsedMessage, ParserFailure
from collector.parsers.card import parse_card_element
from collector.parsers.file import parse_file_element
from collector.parsers.forward import parse_forward_element
from collector.parsers.image import parse_image_element
from collector.parsers.protobuf import decode_message_payload, json_safe
from collector.parsers.reply import parse_reply_element
from collector.parsers.system import parse_system_element
from collector.parsers.text import parse_at_element, parse_face_element, parse_text_element
from collector.parsers.video import parse_video_element
from collector.parsers.voice import parse_voice_element
from collector.qqnt.versions.base import SourceMessageRow
from collector.sync.queue import CollectorQueue
from collector.sync.state import CollectorStateStore


@dataclass(frozen=True)
class MessageParserContext:
    account_id: str
    qq_version: str | None = None
    schema_version: str | None = None
    max_forward_depth: int = 3
    sender_nicknames: Mapping[str, str] | None = None
    room_names: Mapping[str, str] | None = None
    sender_avatars: Mapping[str, str] | None = None
    room_avatars: Mapping[str, str] | None = None


def _first(raw: dict[str, Any], *names: str) -> Any:
    lowered = {str(key).lower(): value for key, value in raw.items()}
    for name in names:
        if name.lower() in lowered:
            return lowered[name.lower()]
    return None


def _chat_type(row: SourceMessageRow) -> str:
    value = row.chat_type.lower()
    if value in {"group", "private"}:
        return value
    table = row.source_table.lower()
    if "group" in table:
        return "group"
    if "c2c" in table or "private" in table or "friend" in table:
        return "private"
    return "group" if value in {"2", "guild"} else "private"


def _timestamp(value: int) -> int:
    return value // 1000 if value > 100_000_000_000 else value


def _blob_base64(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(value)).decode("ascii")
    return None


def _source_identity(context: MessageParserContext, row: SourceMessageRow, chat_type: str) -> tuple[str, str]:
    # Falling back to a constant made every row that lacks msgRandom share one
    # identity component, so two real messages could collapse onto one id. The
    # source primary key is unique within its table by definition, which is what
    # the fallback needs to be.
    msg_random = _first(row.raw_columns, "msgRandom", "msg_random", "random")
    msg_random = str(msg_random) if msg_random not in (None, "") else f"row:{row.source_primary_key}"
    identity = (
        f"qqnt:{context.account_id}:{chat_type}:{row.conversation_id}:"
        f"{row.msg_id}:{msg_random}:{row.msg_seq}"
    )
    return identity, hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _elements(document: Any) -> list[dict[str, Any]] | None:
    if isinstance(document, list):
        values = document
    elif isinstance(document, dict) and isinstance(document.get("elements"), list):
        values = document["elements"]
    elif isinstance(document, dict) and document.get("type"):
        values = [document]
    else:
        return None
    return [value for value in values if isinstance(value, dict)]



def _numeric_identity(raw: dict[str, Any], *names: str) -> str | None:
    value = _first(raw, *names)
    normalized = str(value or "").strip()
    return normalized if normalized.isdigit() and 5 <= len(normalized) <= 12 else None


def _qqnt_identity(row: SourceMessageRow, context: MessageParserContext) -> tuple[str | None, str | None, bool | None]:
    chat_type = _chat_type(row)
    canonical_room = _numeric_identity(
        row.raw_columns, "canonical_room_qq", "room_qq", "group_id", "peer_uin", "conversation_qq", "40030",
    )
    if canonical_room is None and str(row.conversation_id).isdigit() and 5 <= len(str(row.conversation_id)) <= 12:
        canonical_room = str(row.conversation_id)
    is_outgoing: bool | None = None
    outgoing_value = _first(row.raw_columns, "is_outgoing", "outgoing")
    if isinstance(outgoing_value, bool):
        is_outgoing = outgoing_value
    elif outgoing_value is not None:
        is_outgoing = str(outgoing_value).strip().lower() in {"1", "true", "yes"}
    if chat_type == "private":
        # 40020/40021 carry private-chat direction: 40020 != 40021 means this
        # account sent the message, 40020 == 40021 means the peer did.
        #
        # The rule is private-only. Group rows reuse both column numbers with
        # different meanings (40020 is the sender uid, 40021 the group uid), so
        # they are essentially never equal there — evaluating this for a group row
        # marks every message, including everyone else's, as sent by the account,
        # and leaves the correct group check below unreachable.
        sender_identity = str(_first(row.raw_columns, "40020") or "").strip()
        conversation_identity = str(_first(row.raw_columns, "40021") or "").strip()
        if is_outgoing is None and sender_identity and conversation_identity:
            is_outgoing = sender_identity != conversation_identity
        if is_outgoing is True:
            canonical_sender = str(context.account_id)
        else:
            canonical_sender = canonical_room or _numeric_identity(
                row.raw_columns, "canonical_sender_qq", "sender_qq", "sender_uin", "sender_qq_number", "qq", "uin",
            )
    else:
        canonical_sender = _numeric_identity(
            row.raw_columns, "canonical_sender_qq", "sender_qq", "sender_uin", "sender_qq_number", "40033",
        )
        sender_qq = canonical_sender or _numeric_identity(row.raw_columns, "qq", "uin")
        if is_outgoing is None and sender_qq is not None:
            is_outgoing = sender_qq == str(context.account_id)
    return canonical_sender, canonical_room, is_outgoing

def _canonical_segment(element: dict[str, Any]) -> dict[str, Any]:
    raw_type = str(element.get("type") or element.get("kind") or element.get("element_type") or "unknown").lower()
    aliases = {"quote": "reply", "reply_message": "reply", "mention": "at", "mention_user": "at", "text_plain": "text"}
    part_type = aliases.get(raw_type, raw_type)
    data = dict(element.get("data")) if isinstance(element.get("data"), dict) else {
        key: value for key, value in element.items() if key not in {"type", "kind", "element_type"}
    }
    if part_type == "reply":
        reply_id = next((data.get(key) for key in ("id", "message_id", "msg_id", "source_message_id", "msgId") if data.get(key) is not None), None)
        if reply_id is not None:
            data["id"] = str(reply_id)
    elif part_type == "at":
        target_id = next((data.get(key) for key in ("qq", "user_id", "uid", "uin", "target_id", "targetUid") if data.get(key) is not None), None)
        if target_id is not None:
            data["qq"] = str(target_id)
    elif part_type == "text" and data.get("text") is None and data.get("content") is not None:
        data["text"] = str(data["content"])
    return {"type": part_type[:20], "data": data}

def parse_source_message(
    row: SourceMessageRow,
    context: MessageParserContext,
    *,
    resolver: MediaResolver | None = None,
) -> ParsedMessage:
    media_resolver = resolver or MediaResolver()
    decoded = decode_message_payload(row.content)
    failures: list[ParserFailure] = []
    uploads: list[ParsedMediaUpload] = []
    artifacts: list[GeneratedArtifact] = []
    media_references: list[dict[str, Any]] = []
    segments: list[str] = []
    media_ordinal = 0

    element_values = _elements(decoded.document)
    if element_values is None:
        source_key_hash = hashlib.sha256(row.source_primary_key.encode('utf-8')).hexdigest()[:16]
        content_type = _first(row.raw_columns, '40027', 'content_type', 'msg_type')
        if row.content is None and content_type is not None:
            segments.append(f'[QQNT:system,type={content_type}]')
            failures.append(ParserFailure('EMPTY_MESSAGE_PAYLOAD', 'QQNT record has no content payload; preserved its message type'))
        else:
            segments.append(f'[QQNT:unparsed,source_table={row.source_table},source_key_hash={source_key_hash}]')
            failures.append(ParserFailure(decoded.error_code or 'MESSAGE_PARSE_FAILED', 'message payload is not a supported JSON/text envelope'))
    else:
        for element_index, element in enumerate(element_values):
            element_type = str(element.get("type") or element.get("element_type") or "unknown").lower()
            if element_type in {"text", "plain", "emoji_text"}:
                result = parse_text_element(element)
            elif element_type in {"face", "emoji", "system_face"}:
                result = parse_face_element(element)
            elif element_type in {"at", "mention", "mention_user"}:
                result = parse_at_element(element)
            elif element_type in {"image", "imageelement"}:
                result = parse_image_element(element, ordinal=media_ordinal, resolver=media_resolver)
                media_ordinal += 1
            elif element_type in {"voice", "record", "ptt", "voiceelement"}:
                result = parse_voice_element(element, ordinal=media_ordinal, resolver=media_resolver)
                media_ordinal += 1
            elif element_type in {"video", "videoelement"}:
                result = parse_video_element(element, ordinal=media_ordinal, resolver=media_resolver)
                media_ordinal += 1
            elif element_type in {"file", "fileelement"}:
                result = parse_file_element(element, ordinal=media_ordinal, resolver=media_resolver)
                media_ordinal += 1
            elif element_type in {"json", "ark", "json_card"}:
                result = parse_card_element(element, card_type="json")
            elif element_type in {"xml", "xml_card"}:
                result = parse_card_element(element, card_type="xml")
            elif element_type in {"reply", "quote"}:
                result = parse_reply_element(element)
            elif element_type in {"forward", "merged_forward"}:
                result = parse_forward_element(
                    element,
                    ordinal=element_index,
                    max_depth=context.max_forward_depth,
                )
            elif element_type in {"system", "recall", "revoke", "gray_tip"}:
                result = parse_system_element(element)
            else:
                result = parse_system_element({"event_type": f"unknown_element:{element_type}"})
                result.failures.append(ParserFailure("MESSAGE_PARSE_FAILED", f"unsupported element type: {element_type}"))
            if result.text:
                segments.append(result.text)
            if result.media_reference is not None:
                media_references.append(result.media_reference)
            uploads.extend(result.uploads)
            artifacts.extend(result.artifacts)
            failures.extend(result.failures)

    raw_message = "".join(segments) or "[QQNT:empty_message]"
    chat_type = _chat_type(row)
    source_identity, message_id = _source_identity(context, row, chat_type)
    raw_columns = json_safe(row.raw_columns)
    raw_40800 = _blob_base64(_first(row.raw_columns, "raw_40800_protobuf", "40800", "msg_40800"))
    raw_40900 = _blob_base64(_first(row.raw_columns, "raw_40900_protobuf", "40900", "msg_40900"))
    nickname = _first(
        row.raw_columns,
        "nickname",
        "sender_name",
        "send_nick_name",
        "40090",
        "40093",
    )
    if not nickname and context.sender_nicknames:
        nickname = context.sender_nicknames.get(str(row.sender_id))
    sender_avatar = context.sender_avatars.get(str(row.sender_id)) if context.sender_avatars else None
    room_avatar = context.room_avatars.get(str(row.conversation_id)) if context.room_avatars else None
    sender_id = str(row.sender_id or "").strip()
    canonical_sender_id, canonical_room_id, is_outgoing = _qqnt_identity(row, context)
    if not sender_id:
        sender_id = "unknown:" + hashlib.sha256(f"{row.source_table}:{row.source_primary_key}".encode("utf-8")).hexdigest()[:32]
        failures.append(ParserFailure("MISSING_SENDER_ID", "QQNT record has no sender id; generated a stable placeholder"))
    item = {
        "message": {
            "robot_id": context.account_id,
            "platform": "qq",
            "room_id": row.conversation_id,
            "message_type": chat_type,
            "sender_id": sender_id,
            "canonical_sender_id": canonical_sender_id,
            "canonical_room_id": canonical_room_id,
            "is_outgoing": is_outgoing,
            "source_event_type": "qqnt_local_db",
            "message_segments": [_canonical_segment(value) for value in (element_values or [])],
            "nickname": str(nickname) if nickname else None,
            "raw_message": raw_message,
            "local_message": raw_message,
            "timestamp": _timestamp(row.msg_time),
            "source_sequence": max(0, int(row.msg_seq)),
            "message_id": message_id,
        },
        "source_record": {
            "source_table": row.source_table,
            "source_key": row.source_primary_key,
            "platform_message_id": row.msg_id or None,
            "schema_version": context.schema_version,
            "raw_columns": raw_columns,
            "raw_40800_protobuf": raw_40800 or decoded.raw_base64,
            "raw_40900_protobuf": raw_40900,
            "metadata": {
                "source_identity": source_identity,
                "qq_version": context.qq_version,
                "parser_failures": [failure.error_code for failure in failures],
                "room_name": (
                    context.room_names.get(str(row.conversation_id))
                    if context.room_names
                    else None
                ),
                "sender_avatar_url": sender_avatar,
                "room_avatar_url": room_avatar,
                "identity_confidence": "high" if canonical_sender_id or canonical_room_id else "unresolved",
                "identity_fields": {
                    key: raw_columns.get(key)
                    for key in ("40020", "40021", "40030", "40033", "sender_qq", "room_qq")
                    if key in raw_columns
                },
            },
        },
        "media": media_references,
    }
    return ParsedMessage(item, tuple(uploads), tuple(artifacts), tuple(failures))


def enqueue_parsed_message(
    parsed: ParsedMessage,
    *,
    source_id: str,
    queue: CollectorQueue,
    store: CollectorStateStore,
    staging: MediaStagingStore,
    force_refresh: bool = False,
) -> str:
    item = copy.deepcopy(parsed.item)
    failures = list(parsed.failures)
    staged_uploads: list[tuple[ParsedMediaUpload, Any]] = []
    staged_artifacts: list[tuple[GeneratedArtifact, Any]] = []
    for upload in parsed.uploads:
        try:
            staged_uploads.append((upload, staging.stage(upload.path)))
        except StagingError as exc:
            reference = next(
                (entry for entry in item.get("media", []) if entry.get("ordinal") == upload.ordinal),
                None,
            )
            if reference is not None and upload.media_role == "asset":
                reference.update(
                    {
                        "source_state": "downloaded",
                        "archive_state": "failed",
                        "failure_code": str(exc) if str(exc).startswith("MEDIA_") else "MEDIA_UPLOAD_FAILED",
                    }
                )
                reference.pop("asset_file_hash", None)
            elif reference is not None:
                reference.update({"archive_state": "metadata_only", "failure_code": "MEDIA_UPLOAD_FAILED"})
                reference.pop("thumbnail_file_hash", None)
            failures.append(ParserFailure("MEDIA_STAGING_FULL" if "STAGING_FULL" in str(exc) else "MEDIA_UPLOAD_FAILED", str(exc)))
    for artifact in parsed.artifacts:
        try:
            staged_artifacts.append((artifact, staging.stage_bytes(artifact.content, file_name=artifact.file_name)))
        except StagingError as exc:
            for field in ("raw_message", "local_message"):
                item["message"][field] = item["message"][field].replace(artifact.replace_token, artifact.fallback_text)
            failures.append(ParserFailure("MEDIA_STAGING_FULL" if "STAGING_FULL" in str(exc) else "MEDIA_UPLOAD_FAILED", str(exc)))

    dedupe_key = str(item["source_record"]["metadata"]["source_identity"])
    message_queue_id = queue.enqueue_message(
        source_id=source_id,
        dedupe_key=dedupe_key,
        payload=item,
        force_refresh=force_refresh,
    )
    for upload, staged in staged_uploads:
        queue.enqueue_media(
            source_id=source_id,
            message_queue_id=message_queue_id,
            ordinal=upload.ordinal,
            media_role=upload.media_role,
            media_type=upload.media_type,
            staging_path=str(staged.path),
            file_hash=staged.file_hash,
            file_size=staged.file_size,
            content_sha256=staged.content_sha256,
            file_name=upload.file_name,
            force_refresh=force_refresh,
        )
    for artifact, staged in staged_artifacts:
        queue.enqueue_media(
            source_id=source_id,
            message_queue_id=message_queue_id,
            ordinal=artifact.ordinal,
            media_role="artifact",
            media_type="file",
            staging_path=str(staged.path),
            file_hash=staged.file_hash,
            file_size=staged.file_size,
            content_sha256=staged.content_sha256,
            file_name=artifact.file_name,
            payload={
                "replace_token": artifact.replace_token,
                "replacement_template": artifact.replacement_template,
            },
        )
    source_key_hash = hashlib.sha256(str(item["source_record"]["source_key"]).encode("utf-8")).hexdigest()
    source_table = str(item["source_record"]["source_table"])
    if not any(failure.error_code in {"PROTOBUF_PARSE_FAILED", "MESSAGE_PARSE_FAILED"} for failure in failures):
        store.resolve_parser_failures(
            source_id=source_id,
            source_table=source_table,
            source_key_hash=source_key_hash,
        )
    for failure in failures:
        store.record_parser_failure(
            source_id=source_id,
            source_table=source_table,
            source_key_hash=source_key_hash,
            error_code=failure.error_code,
            detail=failure.detail,
        )
    return message_queue_id
