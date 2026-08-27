from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.storage_paths import count_local_storage_paths, is_local_storage_path
from app.import_contract import (
    can_transition_media_state,
    canonical_payload_hash,
    stable_import_source_id,
    validate_media_reference_assets,
)
from app.models import (
    BotProfile,
    IdentityAlias,
    ProfileChangeRecord,
    ImportBatch,
    ImportBatchChunk,
    ImportSource,
    MediaAsset,
    Message,
    MessageMediaReference,
    MessageSourceRecord,
    RobotMessage,
    RoomProfile,
    UserProfile,
)
from app.schemas import (
    ImportBatchCreateRequest,
    ImportBatchMessagesRequest,
    ImportBatchMessagesResponse,
    ImportBatchItemResponse,
    ImportMessageItemRequest,
    ImportSourceCreateRequest,
    MessageMediaReferenceRequest,
)
from app.services.audit_log_service import sanitize_audit_detail
from app.services.message_service import MessageService
from app.time_utils import utc_now


class ImportServiceError(ValueError):
    pass


class ImportNotFoundError(ImportServiceError):
    pass


class ImportConflictError(ImportServiceError):
    pass


class ImportStateError(ImportServiceError):
    pass


def _strip_nul(value: Any) -> Any:
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, dict):
        return {key: _strip_nul(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_strip_nul(item) for item in value]
    return value


def _json_dumps(value: Any) -> str:
    return json.dumps(_strip_nul(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_loads(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _merge_json(existing: str | None, incoming: dict[str, Any]) -> tuple[str | None, bool]:
    if not incoming:
        return existing, False
    merged = _json_loads(existing)
    sanitized = sanitize_audit_detail(incoming)
    changed = any(merged.get(key) != value for key, value in sanitized.items())
    merged.update(sanitized)
    return _json_dumps(merged), changed


def import_source_to_dict(source: ImportSource) -> dict[str, Any]:
    return {
        "id": source.id,
        "source_type": source.source_type,
        "platform": source.platform,
        "account_id": source.account_id,
        "device_id": source.device_id,
        "device_name": source.device_name,
        "qq_version": source.qq_version,
        "schema_version": source.schema_version,
        "status": source.status,
        "first_seen_at": source.first_seen_at,
        "last_seen_at": source.last_seen_at,
        "metadata": _json_loads(source.metadata_json),
    }


def import_batch_to_dict(batch: ImportBatch) -> dict[str, Any]:
    return {
        "id": batch.id,
        "source_id": batch.source_id,
        "mode": batch.mode,
        "status": batch.status,
        "started_at": batch.started_at,
        "completed_at": batch.completed_at,
        "scanned_messages": batch.scanned_messages,
        "inserted_messages": batch.inserted_messages,
        "updated_messages": batch.updated_messages,
        "skipped_messages": batch.skipped_messages,
        "uploaded_media": batch.uploaded_media,
        "not_downloaded_media": batch.not_downloaded_media,
        "missing_media": batch.missing_media,
        "failed_media": batch.failed_media,
        "detail": _json_loads(batch.detail_json),
    }


class ImportService:
    @staticmethod
    async def upsert_source(db: AsyncSession, payload: ImportSourceCreateRequest) -> tuple[ImportSource, bool]:
        result = await db.execute(
            select(ImportSource).where(
                ImportSource.source_type == payload.source_type,
                ImportSource.account_id == payload.account_id,
                ImportSource.device_id == payload.device_id,
            )
        )
        source = result.scalar_one_or_none()
        source_id = payload.id or stable_import_source_id(
            source_type=payload.source_type,
            account_id=payload.account_id,
            device_id=payload.device_id,
        )
        now = utc_now()
        created = source is None
        if source is None:
            conflicting = await db.get(ImportSource, source_id)
            if conflicting is not None:
                raise ImportConflictError("import source id is already used by another source")
            source = ImportSource(
                id=source_id,
                source_type=payload.source_type,
                platform=payload.platform,
                account_id=payload.account_id,
                device_id=payload.device_id,
                device_name=payload.device_name,
                qq_version=payload.qq_version,
                schema_version=payload.schema_version,
                status=payload.status,
                first_seen_at=now,
                last_seen_at=now,
                metadata_json=_json_dumps(sanitize_audit_detail(payload.metadata)) if payload.metadata else None,
            )
            db.add(source)
        else:
            if payload.id and payload.id != source.id:
                raise ImportConflictError("source identity is already registered with a different id")
            source.platform = payload.platform
            source.device_name = payload.device_name or source.device_name
            source.qq_version = payload.qq_version or source.qq_version
            source.schema_version = payload.schema_version or source.schema_version
            source.status = payload.status
            source.last_seen_at = now
            source.metadata_json, _ = _merge_json(source.metadata_json, payload.metadata)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            result = await db.execute(
                select(ImportSource).where(
                    ImportSource.source_type == payload.source_type,
                    ImportSource.account_id == payload.account_id,
                    ImportSource.device_id == payload.device_id,
                )
            )
            source = result.scalar_one_or_none()
            if source is None:
                raise
            if payload.id and payload.id != source.id:
                raise ImportConflictError("source identity is already registered with a different id")
            created = False
        await db.refresh(source)
        return source, created

    @staticmethod
    async def create_batch(db: AsyncSession, payload: ImportBatchCreateRequest) -> tuple[ImportBatch, bool]:
        source = await db.get(ImportSource, payload.source_id)
        if source is None:
            raise ImportNotFoundError("import source not found")
        batch_id = payload.id or uuid.uuid4().hex
        batch = await db.get(ImportBatch, batch_id)
        if batch is not None:
            if batch.source_id != payload.source_id or batch.mode != payload.mode:
                raise ImportConflictError("import batch id is already used with different attributes")
            return batch, False
        batch = ImportBatch(
            id=batch_id,
            source_id=payload.source_id,
            mode=payload.mode,
            status="running",
            started_at=utc_now(),
            detail_json=_json_dumps(sanitize_audit_detail(payload.detail)) if payload.detail else None,
        )
        db.add(batch)
        source.last_seen_at = utc_now()
        await db.commit()
        await db.refresh(batch)
        return batch, True

    @staticmethod
    async def finish_batch(
        db: AsyncSession,
        *,
        batch_id: str,
        failed: bool,
        partial: bool,
        detail: dict[str, Any],
        error_code: str | None = None,
        error_detail: str | None = None,
    ) -> ImportBatch:
        batch_result = await db.execute(
            select(ImportBatch).where(ImportBatch.id == batch_id).with_for_update()
        )
        batch = batch_result.scalar_one_or_none()
        if batch is None:
            raise ImportNotFoundError("import batch not found")
        target_status = "failed" if failed else ("partial" if partial or batch.failed_media > 0 else "completed")
        is_terminal = batch.status in {"completed", "failed", "cancelled"} or (batch.status == "partial" and batch.completed_at is not None)
        if is_terminal:
            if batch.status != target_status:
                raise ImportStateError(f"cannot change terminal batch status {batch.status} to {target_status}")
            return batch
        merged_detail = dict(detail)
        if error_code:
            merged_detail["error_code"] = error_code
        if error_detail:
            merged_detail["error_detail"] = error_detail
        batch.detail_json, _ = _merge_json(batch.detail_json, merged_detail)
        batch.status = target_status
        batch.completed_at = utc_now()
        await db.commit()
        await db.refresh(batch)
        return batch

    @staticmethod
    async def import_batch_messages(
        db: AsyncSession,
        *,
        batch_id: str,
        payload: ImportBatchMessagesRequest,
    ) -> ImportBatchMessagesResponse:
        payload_data = payload.model_dump(mode="json")
        request_hash = canonical_payload_hash(payload_data)
        cached_result = await db.execute(
            select(ImportBatchChunk).where(
                ImportBatchChunk.batch_id == batch_id,
                ImportBatchChunk.request_hash == request_hash,
            )
        )
        cached = cached_result.scalar_one_or_none()
        if cached is not None:
            response = ImportBatchMessagesResponse.model_validate_json(cached.response_json)
            response.replayed = True
            return response

        batch_result = await db.execute(
            select(ImportBatch).where(ImportBatch.id == batch_id).with_for_update()
        )
        batch = batch_result.scalar_one_or_none()
        if batch is None:
            raise ImportNotFoundError("import batch not found")
        cached_result = await db.execute(
            select(ImportBatchChunk).where(
                ImportBatchChunk.batch_id == batch_id,
                ImportBatchChunk.request_hash == request_hash,
            )
        )
        cached = cached_result.scalar_one_or_none()
        if cached is not None:
            response = ImportBatchMessagesResponse.model_validate_json(cached.response_json)
            response.replayed = True
            return response
        if batch.status not in {"running", "partial"} or batch.completed_at is not None:
            raise ImportStateError(f"import batch is not writable in status {batch.status}")
        source_result = await db.execute(
            select(ImportSource).where(ImportSource.id == batch.source_id).with_for_update()
        )
        source = source_result.scalar_one_or_none()
        if source is None:
            raise ImportNotFoundError("import source not found")

        response = ImportBatchMessagesResponse()
        media_counts = {"uploaded": 0, "not_downloaded": 0, "missing": 0, "failed": 0}
        for index, item in enumerate(payload.messages):
            try:
                last_integrity_error: IntegrityError | None = None
                for attempt in range(2):
                    try:
                        async with db.begin_nested():
                            msg_hash, item_status, item_media_counts = await ImportService._upsert_message_item(
                                db,
                                source=source,
                                batch=batch,
                                item=item,
                            )
                            await db.flush()
                        last_integrity_error = None
                        break
                    except IntegrityError as exc:
                        last_integrity_error = exc
                        if attempt == 0:
                            await asyncio.sleep(0)
                            continue
                        raise
                if last_integrity_error is not None:
                    raise last_integrity_error
                setattr(response, item_status, getattr(response, item_status) + 1)
                for key, value in item_media_counts.items():
                    media_counts[key] += value
                response.items.append(
                    ImportBatchItemResponse(
                        index=index,
                        message_id=item.message.message_id,
                        msg_hash=msg_hash,
                        status=item_status,
                    )
                )
            except (ImportServiceError, ValueError, IntegrityError) as exc:
                response.failed += 1
                response.items.append(
                    ImportBatchItemResponse(
                        index=index,
                        message_id=item.message.message_id,
                        status="failed",
                        error=str(exc),
                    )
                )

        batch.scanned_messages += len(payload.messages)
        batch.inserted_messages += response.inserted
        batch.updated_messages += response.updated
        batch.skipped_messages += response.unchanged
        batch.uploaded_media += media_counts["uploaded"]
        batch.not_downloaded_media += media_counts["not_downloaded"]
        batch.missing_media += media_counts["missing"]
        batch.failed_media += media_counts["failed"] + response.failed
        if response.failed:
            batch.status = "partial"

        chunk = ImportBatchChunk(
            batch_id=batch_id,
            request_hash=request_hash,
            response_json=response.model_dump_json(),
        )
        db.add(chunk)
        source.last_seen_at = utc_now()
        await db.commit()
        return response

    @staticmethod
    async def _upsert_message_item(
        db: AsyncSession,
        *,
        source: ImportSource,
        batch: ImportBatch,
        item: ImportMessageItemRequest,
    ) -> tuple[str, str, dict[str, int]]:
        message_data = item.message.model_copy(
            update={
                "nickname": _strip_nul(item.message.nickname),
                "raw_message": _strip_nul(item.message.raw_message),
                "local_message": _strip_nul(item.message.local_message),
            }
        )
        if source.platform != message_data.platform:
            raise ImportConflictError("message platform does not match the import source platform")
        if source.account_id != message_data.robot_id:
            raise ImportConflictError("message robot_id does not match the import source account_id")
        event_identity = message_data.message_id
        if not event_identity:
            raise ImportConflictError("Collector batch message_id is required")
        canonical_sender_id = str(message_data.canonical_sender_id or message_data.sender_id)
        canonical_room_id = str(message_data.canonical_room_id or message_data.room_id)
        incoming_local = message_data.local_message or message_data.raw_message
        msg_hash = MessageService.message_hash(
            platform=message_data.platform,
            room_id=canonical_room_id,
            sender_id=canonical_sender_id,
            event_identity=str(event_identity),
            message_type=message_data.message_type,
        )

        message_result = await db.execute(select(Message).where(Message.msg_hash == msg_hash).with_for_update())
        message = message_result.scalar_one_or_none()
        if message is not None and message.message_type != message_data.message_type:
            msg_hash = MessageService.generate_md5(f"{msg_hash}_{message_data.message_type}".encode("utf-8"))
            message_result = await db.execute(select(Message).where(Message.msg_hash == msg_hash).with_for_update())
            message = message_result.scalar_one_or_none()
        if message is None and message_data.message_id:
            existing_result = await db.execute(
                select(Message).where(
                    Message.platform == message_data.platform,
                    Message.room_id == canonical_room_id,
                    Message.sender_id == canonical_sender_id,
                    Message.message_type == message_data.message_type,
                    Message.external_message_id == message_data.message_id,
                ).with_for_update()
            )
            message = existing_result.scalars().first()
            if message is not None:
                msg_hash = message.msg_hash

        if message is None:
            alias_result = await db.execute(
                select(Message)
                .join(MessageSourceRecord, MessageSourceRecord.msg_hash == Message.msg_hash)
                .where(
                    Message.platform == message_data.platform,
                    Message.room_id == canonical_room_id,
                    Message.sender_id == canonical_sender_id,
                    MessageSourceRecord.source_external_message_id == message_data.message_id,
                )
                .with_for_update()
            )
            message = alias_result.scalars().first()
            if message is not None:
                msg_hash = message.msg_hash

        platform_message_id = ImportService._platform_message_id(item)
        if message is None and platform_message_id:
            platform_result = await db.execute(
                select(Message).where(
                    Message.platform == message_data.platform,
                    Message.room_id == canonical_room_id,
                    Message.sender_id == canonical_sender_id,
                    Message.external_message_id == platform_message_id,
                ).with_for_update()
            )
            message = platform_result.scalars().first()
            if message is not None:
                msg_hash = message.msg_hash

        if message is None:
            # Last-resort semantic match, used to fold a message that arrived from
            # another source (realtime capture) into the same archived row.
            #
            # It must never merge two *different rows of the same source database*:
            # sending the same short text twice within one second is ordinary in a
            # group, and those rows are separate messages even though session,
            # sender, second and text all match. Without the guard below the second
            # row attaches its source record to the first message and is never
            # stored, which loses a message silently.
            same_source_other_row = (
                select(MessageSourceRecord.id)
                .where(
                    MessageSourceRecord.msg_hash == Message.msg_hash,
                    MessageSourceRecord.source_id == source.id,
                    or_(
                        MessageSourceRecord.source_table != item.source_record.source_table,
                        MessageSourceRecord.source_primary_key != item.source_record.source_key,
                    ),
                )
                .exists()
            )
            semantic_conditions = [
                Message.platform == message_data.platform,
                Message.room_id == canonical_room_id,
                Message.message_type == message_data.message_type,
                Message.sender_id == canonical_sender_id,
                Message.timestamp == message_data.timestamp,
                Message.local_message == incoming_local,
                ~same_source_other_row,
            ]
            if message_data.source_sequence is not None:
                # A candidate that already carries a different sequence number is a
                # different message; one with no sequence (realtime capture) may
                # still be the same message seen from another source.
                semantic_conditions.append(
                    or_(
                        Message.source_sequence.is_(None),
                        Message.source_sequence == message_data.source_sequence,
                    )
                )
            semantic_result = await db.execute(
                select(Message)
                .where(*semantic_conditions)
                .order_by(Message.created_at.asc(), Message.msg_hash.asc())
                .limit(1)
                .with_for_update()
            )
            message = semantic_result.scalars().first()
            if message is not None:
                msg_hash = message.msg_hash

        created = message is None
        changed = created
        qqnt_authoritative = source.source_type == "qqnt_local_db"
        if message is None:
            message = Message(
                msg_hash=msg_hash,
                platform=message_data.platform,
                room_id=canonical_room_id,
                message_type=message_data.message_type,
                external_message_id=message_data.message_id,
                sender_id=canonical_sender_id,
                nickname=message_data.nickname,
                raw_message=message_data.raw_message,
                local_message=incoming_local,
                timestamp=message_data.timestamp,
                source_sequence=message_data.source_sequence,
                is_outgoing=message_data.is_outgoing,
            )
            db.add(message)
            await db.flush()
        else:
            if message_data.is_outgoing is not None and message.is_outgoing != message_data.is_outgoing:
                message.is_outgoing = message_data.is_outgoing
                changed = True
            if not message.external_message_id and message_data.message_id:
                message.external_message_id = message_data.message_id
                changed = True
            if message_data.nickname and (not message.nickname or (qqnt_authoritative and message.nickname != message_data.nickname)):
                message.nickname = message_data.nickname
                changed = True
            if ImportService._local_message_score(incoming_local) > ImportService._local_message_score(message.local_message):
                message.local_message = incoming_local
                changed = True
            if message.source_sequence is None and message_data.source_sequence is not None:
                message.source_sequence = message_data.source_sequence
                changed = True

        assoc_result = await db.execute(
            select(RobotMessage).where(
                RobotMessage.robot_id == message_data.robot_id,
                RobotMessage.msg_hash == msg_hash,
            )
        )
        if assoc_result.scalar_one_or_none() is None:
            db.add(RobotMessage(robot_id=message_data.robot_id, msg_hash=msg_hash))
            changed = True

        metadata = item.source_record.metadata or {}
        await ImportService._upsert_identity_alias(
            db, platform=message_data.platform, identity_type="user", canonical_id=canonical_sender_id,
            alias_id=message_data.sender_id, alias_type="source_sender_id", source_id=source.id,
        )
        await ImportService._upsert_identity_alias(
            db, platform=message_data.platform, identity_type="conversation", canonical_id=canonical_room_id,
            alias_id=message_data.room_id, alias_type="source_room_id", source_id=source.id,
        )
        await MessageService._upsert_message_parts(
            db, msg_hash=msg_hash, segments=message_data.message_segments,
            source_format=message_data.source_event_type or source.source_type,
        )
        room_name = metadata.get("room_name")
        room_avatar_url = metadata.get("room_avatar_url")
        sender_avatar_url = metadata.get("sender_avatar_url")
        if message_data.message_type == "group" and (
            (isinstance(room_name, str) and room_name.strip())
            or (isinstance(room_avatar_url, str) and room_avatar_url.strip())
        ):
            room_profile = await db.get(RoomProfile, canonical_room_id)
            if room_profile is None:
                db.add(
                    RoomProfile(
                        room_id=canonical_room_id,
                        platform=message_data.platform,
                        display_name=room_name.strip() if isinstance(room_name, str) and room_name.strip() else None,
                        avatar_path=(room_avatar_url.strip() if isinstance(room_avatar_url, str) and is_local_storage_path(room_avatar_url) else None),
                        avatar_source_url=(room_avatar_url.strip() if isinstance(room_avatar_url, str) and not is_local_storage_path(room_avatar_url) else None),
                        avatar_status="local" if isinstance(room_avatar_url, str) and is_local_storage_path(room_avatar_url) else "pending",
                    )
                )
            else:
                if isinstance(room_name, str) and room_name.strip() and (
                    not room_profile.display_name or (qqnt_authoritative and room_profile.display_name != room_name.strip())
                ):
                    room_profile.display_name = room_name.strip()
                if isinstance(room_avatar_url, str) and room_avatar_url.strip():
                    if is_local_storage_path(room_avatar_url):
                        room_profile.avatar_path = room_avatar_url.strip()
                        room_profile.avatar_status = "local"
                    else:
                        room_profile.avatar_source_url = room_avatar_url.strip()
                        if not room_profile.avatar_path:
                            room_profile.avatar_status = "pending"
        if message_data.nickname or (isinstance(sender_avatar_url, str) and sender_avatar_url.strip()):
            user_profile = await db.get(UserProfile, canonical_sender_id)
            if user_profile is None:
                db.add(
                    UserProfile(
                        user_id=canonical_sender_id,
                        platform=message_data.platform,
                        display_name=message_data.nickname,
                        avatar_path=(sender_avatar_url.strip() if isinstance(sender_avatar_url, str) and is_local_storage_path(sender_avatar_url) else None),
                        avatar_source_url=(sender_avatar_url.strip() if isinstance(sender_avatar_url, str) and not is_local_storage_path(sender_avatar_url) else None),
                        avatar_status="local" if isinstance(sender_avatar_url, str) and is_local_storage_path(sender_avatar_url) else "pending",
                    )
                )
            else:
                if message_data.nickname and (
                    not user_profile.display_name or (qqnt_authoritative and user_profile.display_name != message_data.nickname)
                ):
                    user_profile.display_name = message_data.nickname
                if isinstance(sender_avatar_url, str) and sender_avatar_url.strip():
                    if is_local_storage_path(sender_avatar_url):
                        user_profile.avatar_path = sender_avatar_url.strip()
                        user_profile.avatar_status = "local"
                    else:
                        user_profile.avatar_source_url = sender_avatar_url.strip()
                        if not user_profile.avatar_path:
                            user_profile.avatar_status = "pending"

        profile = await db.get(BotProfile, message_data.robot_id)
        now = utc_now()
        if profile is None:
            db.add(
                BotProfile(
                    id=message_data.robot_id,
                    platform=message_data.platform,
                    display_name=message_data.nickname if message_data.sender_id == message_data.robot_id else None,
                    first_seen_at=now,
                    last_seen_at=now,
                )
            )
        else:
            profile.last_seen_at = now
            if not profile.display_name and message_data.sender_id == message_data.robot_id and message_data.nickname:
                profile.display_name = message_data.nickname

        if await ImportService._upsert_source_record(
            db,
            msg_hash=msg_hash,
            source_id=source.id,
            batch_id=batch.id,
            item=item,
        ):
            changed = True

        media_counts = {"uploaded": 0, "not_downloaded": 0, "missing": 0, "failed": 0}
        local_assets = await ImportService._local_media_assets_by_ordinal(db, message.local_message)
        for reference in item.media:
            if reference.archive_state != "complete" and reference.ordinal < len(local_assets):
                asset = local_assets[reference.ordinal]
                compatible_type = asset is not None and MessageService.media_types_compatible(asset.file_type, reference.media_type)
                if compatible_type and asset.file_size > 0 and not asset.file_type.endswith("_missing"):
                    reference = reference.model_copy(
                        update={
                            "source_state": "downloaded",
                            "archive_state": "complete",
                            "asset_file_hash": asset.file_hash,
                            "actual_file_size": asset.file_size,
                            "failure_code": None,
                            "failure_detail": None,
                        }
                    )
            reference_changed, applied_source_state, applied_archive_state, archived_now = await ImportService._upsert_media_reference(
                db,
                msg_hash=msg_hash,
                reference=reference,
                allow_complete_rebind=qqnt_authoritative,
            )
            changed = changed or reference_changed
            if archived_now:
                media_counts["uploaded"] += 1
            if applied_source_state == "not_downloaded":
                media_counts["not_downloaded"] += 1
            if applied_source_state == "missing":
                media_counts["missing"] += 1
            if applied_archive_state == "failed":
                media_counts["failed"] += 1

        # Parts were written before the media references above existed, so the
        # link between them can only be resolved now.
        await MessageService.link_message_parts_to_media(db, msg_hash)

        return msg_hash, ("inserted" if created else "updated" if changed else "unchanged"), media_counts

    @staticmethod
    async def _upsert_source_record(
        db: AsyncSession,
        *,
        msg_hash: str,
        source_id: str,
        batch_id: str,
        item: ImportMessageItemRequest,
    ) -> bool:
        incoming = item.source_record
        result = await db.execute(
            select(MessageSourceRecord).where(
                MessageSourceRecord.source_id == source_id,
                MessageSourceRecord.source_table == incoming.source_table,
                MessageSourceRecord.source_primary_key == incoming.source_key,
            )
        )
        record = result.scalar_one_or_none()
        now = utc_now()
        if record is None:
            db.add(
                MessageSourceRecord(
                    msg_hash=msg_hash,
                    source_id=source_id,
                    batch_id=batch_id,
                    source_table=incoming.source_table,
                    source_primary_key=incoming.source_key,
                    source_external_message_id=item.message.message_id,
                    platform_message_id=ImportService._platform_message_id(item),
                    schema_version=incoming.schema_version,
                    raw_columns_json=_json_dumps(incoming.raw_columns) if incoming.raw_columns else None,
                    raw_40800_protobuf=incoming.raw_40800_protobuf,
                    raw_40900_protobuf=incoming.raw_40900_protobuf,
                    metadata_json=_json_dumps(sanitize_audit_detail(incoming.metadata)) if incoming.metadata else None,
                    imported_at=now,
                    last_seen_at=now,
                )
            )
            return True
        if record.msg_hash != msg_hash:
            raise ImportConflictError("source record is already bound to a different canonical message")

        changed = False
        record.batch_id = batch_id
        record.last_seen_at = now
        for attr, value in (
            ("source_external_message_id", item.message.message_id),
            ("platform_message_id", ImportService._platform_message_id(item)),
            ("schema_version", incoming.schema_version),
            ("raw_40800_protobuf", incoming.raw_40800_protobuf),
            ("raw_40900_protobuf", incoming.raw_40900_protobuf),
        ):
            if value is not None and getattr(record, attr) != value:
                setattr(record, attr, value)
                changed = True
        if incoming.raw_columns:
            raw_columns = _json_dumps(incoming.raw_columns)
            if record.raw_columns_json != raw_columns:
                record.raw_columns_json = raw_columns
                changed = True
        record.metadata_json, metadata_changed = _merge_json(record.metadata_json, incoming.metadata)
        return changed or metadata_changed

    @staticmethod
    async def _upsert_media_reference(
        db: AsyncSession,
        *,
        msg_hash: str,
        reference: MessageMediaReferenceRequest,
        allow_complete_rebind: bool = False,
    ) -> tuple[bool, str, str, bool]:
        validate_media_reference_assets(
            source_state=reference.source_state,
            archive_state=reference.archive_state,
            asset_file_hash=reference.asset_file_hash,
            thumbnail_file_hash=reference.thumbnail_file_hash,
        )
        for label, file_hash in (
            ("asset_file_hash", reference.asset_file_hash),
            ("thumbnail_file_hash", reference.thumbnail_file_hash),
        ):
            if file_hash:
                asset = await db.get(MediaAsset, file_hash)
                if asset is None:
                    raise ImportConflictError(f"{label} does not reference an existing media asset")
                if asset.file_size <= 0:
                    raise ImportConflictError(f"{label} references an empty media asset")

        result = await db.execute(
            select(MessageMediaReference).where(
                MessageMediaReference.msg_hash == msg_hash,
                MessageMediaReference.ordinal == reference.ordinal,
            ).with_for_update()
        )
        model = result.scalar_one_or_none()
        now = utc_now()
        if model is None:
            model = MessageMediaReference(
                msg_hash=msg_hash,
                ordinal=reference.ordinal,
                media_type=reference.media_type,
                source_state=reference.source_state,
                archive_state=reference.archive_state,
                first_seen_at=now,
                last_checked_at=now,
            )
            ImportService._apply_media_fields(
                model,
                reference,
                overwrite=True,
                allow_failure=reference.archive_state == "failed",
            )
            model.archived_at = now if reference.archive_state == "complete" else None
            db.add(model)
            return True, model.source_state, model.archive_state, model.archive_state == "complete"

        changed = False
        was_complete = model.archive_state == "complete"
        rebinding_complete = (
            was_complete
            and reference.asset_file_hash
            and model.asset_file_hash
            and reference.asset_file_hash != model.asset_file_hash
        )
        if rebinding_complete and not allow_complete_rebind:
            raise ImportConflictError("a complete media reference cannot be rebound to a different asset")
        transition_allowed = (
            True
            if rebinding_complete and allow_complete_rebind
            else can_transition_media_state(
                model.source_state,
                model.archive_state,
                reference.source_state,
                reference.archive_state,
            )
        )
        if transition_allowed and (model.source_state, model.archive_state) != (reference.source_state, reference.archive_state):
            model.source_state = reference.source_state
            model.archive_state = reference.archive_state
            changed = True
        overwrite = transition_allowed and (not was_complete or bool(rebinding_complete and allow_complete_rebind))
        changed = ImportService._apply_media_fields(
            model,
            reference,
            overwrite=overwrite,
            allow_failure=transition_allowed and reference.archive_state == "failed",
        ) or changed
        if transition_allowed and reference.archive_state == "complete":
            if model.archived_at is None:
                model.archived_at = now
                changed = True
            if model.failure_code is not None or model.failure_detail is not None:
                model.failure_code = None
                model.failure_detail = None
                changed = True
        model.last_checked_at = now
        return changed, model.source_state, model.archive_state, (not was_complete and model.archive_state == "complete")

    @staticmethod
    async def _upsert_identity_alias(
        db: AsyncSession,
        *,
        platform: str,
        identity_type: str,
        canonical_id: str,
        alias_id: str,
        alias_type: str,
        source_id: str | None,
    ) -> None:
        if not alias_id:
            return
        result = await db.execute(
            select(IdentityAlias).where(
                IdentityAlias.platform == platform,
                IdentityAlias.identity_type == identity_type,
                IdentityAlias.alias_id == alias_id,
                IdentityAlias.source_id == source_id,
            ).with_for_update()
        )
        alias = result.scalar_one_or_none()
        if alias is None:
            db.add(IdentityAlias(
                platform=platform, identity_type=identity_type, canonical_id=canonical_id,
                alias_id=alias_id, alias_type=alias_type, source_id=source_id, confidence="observed",
            ))
        else:
            alias.canonical_id = canonical_id
            alias.alias_type = alias_type
            alias.last_seen_at = utc_now()

    @staticmethod
    def _apply_media_fields(
        model: MessageMediaReference,
        reference: MessageMediaReferenceRequest,
        *,
        overwrite: bool,
        allow_failure: bool,
    ) -> bool:
        changed = False
        for attr in (
            "media_type",
            "asset_file_hash",
            "thumbnail_file_hash",
            "file_name",
            "file_ext",
            "declared_file_size",
            "actual_file_size",
            "source_md5",
            "source_sha1",
            "source_uuid",
            "content_sha256",
            "source_local_path",
            "duration_ms",
            "width",
            "height",
        ):
            incoming = getattr(reference, attr)
            current = getattr(model, attr)
            if incoming is not None and (current is None or overwrite) and current != incoming:
                setattr(model, attr, incoming)
                changed = True
        if allow_failure:
            for attr in ("failure_code", "failure_detail"):
                incoming = getattr(reference, attr)
                current = getattr(model, attr)
                if incoming is not None and (current is None or overwrite) and current != incoming:
                    setattr(model, attr, incoming)
                    changed = True
        model.metadata_json, metadata_changed = _merge_json(model.metadata_json, reference.metadata)
        return changed or metadata_changed

    @staticmethod
    def _local_message_score(value: str | None) -> tuple[int, int]:
        text = value or ""
        return (count_local_storage_paths(text), len(text))

    @staticmethod
    def _platform_message_id(item: ImportMessageItemRequest) -> str | None:
        if item.source_record.platform_message_id:
            return item.source_record.platform_message_id
        for container in (item.source_record.raw_columns, item.source_record.metadata):
            for key in ("platform_message_id", "message_id", "msg_id", "msgId"):
                value = container.get(key)
                if value is not None:
                    normalized = str(value).strip()
                    if normalized:
                        return normalized[:64]
        return None

    @staticmethod
    async def _local_media_assets_by_ordinal(db: AsyncSession, local_message: str | None) -> list[MediaAsset | None]:
        paths_by_ordinal = MessageService.local_media_paths_by_ordinal(
            local_message,
            get_settings().public_storage_prefix,
        )
        local_paths = [path for path in paths_by_ordinal if path]
        if not local_paths:
            return []
        result = await db.execute(select(MediaAsset).where(MediaAsset.local_path.in_(local_paths)))
        by_path = {asset.local_path: asset for asset in result.scalars().all()}
        return [by_path.get(path or "") for path in paths_by_ordinal]
