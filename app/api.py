import asyncio
import json
import re
import time
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.wechat_pc import normalize_wechat_event
from app.config import Settings, get_settings
from app.database import get_db_session
from app.schemas import (
    AdapterCreateRequest,
    AdapterResponse,
    AdapterUpdateRequest,
    AuditLogResponse,
    BackupRunResponse,
    BackupStatusResponse,
    BotProfileResponse,
    DashboardResponse,
    ImportResultResponse,
    ImportValidationResponse,
    MediaBackfillResponse,
    MessageIngestRequest,
    MessageIngestResponse,
    MessageResponse,
    OfflineAuditResponse,
    OfflineRepairResponse,
    RoomResponse,
)
from app.services.adapter_service import AdapterService
from app.services.audit_log_service import AuditLogService
from app.services.bot_profile_service import BotProfileService
from app.services.backup_service import BackupService
from app.services.dashboard_service import DashboardService
from app.services.media_backfill_service import MediaBackfillService
from app.services.media_service import MediaService, _build_cq_segment, _parse_cq_params
from app.services.message_service import MessageService
from app.services.offline_audit_service import OfflineAuditService
from app.services.offline_repair_service import OfflineRepairService
from app.services.onebot_rpc_service import OneBotRPCService
from app.services.profile_placeholder_service import ProfilePlaceholderService
from app.services.query_service import QueryService
from app.services.room_profile_service import RoomProfileService
from app.services.user_profile_service import UserProfileService
from app.models import Message, RobotMessage


_RATE_LIMIT_BUCKETS: dict[tuple[str, str], list[float]] = {}


def _extract_bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token


async def require_admin_api_token(
    request: Request,
    settings: Settings = Depends(get_settings),
    db: AsyncSession = Depends(get_db_session),
) -> None:
    configured_token = settings.admin_api_token.strip()
    if not configured_token:
        return

    header_token = request.headers.get("x-admin-token")
    bearer_token = _extract_bearer_token(request.headers.get("authorization"))
    if header_token == configured_token or bearer_token == configured_token:
        return

    await AuditLogService.record(
        db,
        action="auth.failed",
        status="failed",
        actor="anonymous",
        ip_address=_client_ip(request),
        target=request.url.path,
        detail={"method": request.method},
    )
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid admin api token")


router = APIRouter(dependencies=[Depends(require_admin_api_token)])


def _client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    return request.client.host if request.client else None


def _actor(request: Request) -> str:
    token = request.headers.get("x-admin-token") or _extract_bearer_token(request.headers.get("authorization"))
    if token:
        return "admin-token"
    return "development-open"


def _enforce_high_risk_rate_limit(request: Request, action: str, settings: Settings) -> None:
    limit = settings.high_risk_rate_limit_per_minute
    if limit <= 0:
        return
    now = time.monotonic()
    key = (_client_ip(request) or "unknown", action)
    bucket = [timestamp for timestamp in _RATE_LIMIT_BUCKETS.get(key, []) if now - timestamp < 60]
    if len(bucket) >= limit:
        _RATE_LIMIT_BUCKETS[key] = bucket
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="high risk operation rate limit exceeded")
    bucket.append(now)
    _RATE_LIMIT_BUCKETS[key] = bucket


async def _audit(
    db: AsyncSession,
    request: Request,
    *,
    action: str,
    status_: str,
    target: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    await AuditLogService.record(
        db,
        action=action,
        status=status_,
        actor=_actor(request),
        ip_address=_client_ip(request),
        target=target,
        detail=detail,
    )


@router.get("/adapters", response_model=list[AdapterResponse])
async def list_adapters(db: AsyncSession = Depends(get_db_session)) -> list[AdapterResponse]:
    adapters = await QueryService.list_adapters(db)
    return [AdapterResponse.model_validate(adapter) for adapter in adapters]


@router.get("/bots", response_model=list[BotProfileResponse])
async def list_bots(db: AsyncSession = Depends(get_db_session)) -> list[BotProfileResponse]:
    profiles = await QueryService.list_bot_profiles(db)
    return [BotProfileResponse.model_validate(profile) for profile in profiles]


@router.get("/dashboard", response_model=DashboardResponse)
async def get_dashboard_summary(
    db: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> DashboardResponse:
    return DashboardResponse(**await DashboardService.get_summary(db, backup_root=settings.backup_root))


def _auto_backup_enabled(cron_expr: str) -> bool:
    value = (cron_expr or "").strip().lower()
    return bool(value and value not in {"off", "disabled", "none", "false", "0"})


@router.get("/backup/status", response_model=BackupStatusResponse)
async def get_backup_status(settings: Settings = Depends(get_settings)) -> BackupStatusResponse:
    backup_root = settings.backup_root
    backups = sorted(backup_root.glob("auto-backup-*.json"), key=lambda path: (path.stat().st_mtime, path.name)) if backup_root.exists() else []
    latest = backups[-1].name if backups else None
    return BackupStatusResponse(
        enabled=_auto_backup_enabled(settings.auto_backup_cron),
        cron=settings.auto_backup_cron,
        keep_latest=settings.auto_backup_keep_latest,
        backup_root=str(backup_root),
        backups=len(backups),
        latest_backup=latest,
    )


@router.post("/backup/run", response_model=BackupRunResponse)
async def run_backup_now(
    request: Request,
    db: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> BackupRunResponse:
    action = "backup.run"
    _enforce_high_risk_rate_limit(request, action, settings)
    try:
        path = await BackupService.write_auto_backup_file(
            db,
            backup_root=settings.backup_root,
            storage_root=settings.storage_root,
            public_storage_prefix=settings.public_storage_prefix,
            max_media_bytes=settings.media_max_bytes,
            keep_latest=settings.auto_backup_keep_latest,
            system_id=settings.system_instance_id,
            signing_key=settings.app_secret_key,
        )
    except Exception as exc:
        await _audit(db, request, action=action, status_="failed", detail={"error": str(exc)})
        raise
    await _audit(db, request, action=action, status_="success", target=path.name)
    return BackupRunResponse(path=str(path), filename=path.name)


@router.get("/audit/logs", response_model=list[AuditLogResponse])
async def list_audit_logs(
    action: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db_session),
) -> list[AuditLogResponse]:
    logs = await AuditLogService.list_logs(db, action=action, limit=limit)
    return [AuditLogResponse.model_validate(log) for log in logs]


@router.post("/adapters", response_model=AdapterResponse, status_code=status.HTTP_201_CREATED)
async def create_adapter(
    payload: AdapterCreateRequest,
    db: AsyncSession = Depends(get_db_session),
) -> AdapterResponse:
    try:
        adapter = await AdapterService.create_adapter(
            db,
            adapter_id=payload.id,
            platform=payload.platform,
            config_json=payload.config_json,
            status=payload.status,
            current_robot_id=payload.current_robot_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return AdapterResponse.model_validate(adapter)


@router.patch("/adapters/{adapter_id}", response_model=AdapterResponse)
async def update_adapter(
    adapter_id: str,
    payload: AdapterUpdateRequest,
    db: AsyncSession = Depends(get_db_session),
) -> AdapterResponse:
    adapter = await AdapterService.update_adapter(
        db,
        adapter_id=adapter_id,
        platform=payload.platform,
        config_json=payload.config_json,
        status=payload.status,
        current_robot_id=payload.current_robot_id,
        config_json_provided="config_json" in payload.model_fields_set,
        current_robot_id_provided="current_robot_id" in payload.model_fields_set,
    )
    if adapter is None:
        raise HTTPException(status_code=404, detail="adapter not found")
    return AdapterResponse.model_validate(adapter)


@router.delete("/adapters/{adapter_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_adapter(
    adapter_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> Response:
    action = "adapter.delete"
    _enforce_high_risk_rate_limit(request, action, settings)
    deleted = await AdapterService.delete_adapter(db, adapter_id=adapter_id)
    if not deleted:
        await _audit(db, request, action=action, status_="failed", target=adapter_id, detail={"reason": "not_found"})
        raise HTTPException(status_code=404, detail="adapter not found")
    await _audit(db, request, action=action, status_="success", target=adapter_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/rooms", response_model=list[RoomResponse])
async def list_rooms(
    robot_id: str = Query(..., min_length=1),
    db: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> list[RoomResponse]:
    rooms = await QueryService.list_rooms(db, robot_id=robot_id)
    if await _hydrate_missing_room_profiles(db, robot_id=robot_id, rooms=rooms, settings=settings):
        rooms = await QueryService.list_rooms(db, robot_id=robot_id)
    return [RoomResponse(**room) for room in rooms]


async def _hydrate_missing_room_profiles(db: AsyncSession, robot_id: str, rooms: list[dict], settings: Settings) -> bool:
    missing_group_rooms = [
        room
        for room in rooms
        if room.get("message_type") == "group"
        and str(room.get("room_id") or "").isdigit()
        and (not room.get("display_name") or not room.get("avatar_path"))
    ]
    missing_private_rooms = [
        room
        for room in rooms
        if room.get("message_type") == "private"
        and str(room.get("room_id") or "").isdigit()
        and (not room.get("display_name") or not room.get("avatar_path"))
    ]
    if not missing_group_rooms and not missing_private_rooms:
        return False

    changed = False
    async with httpx.AsyncClient(timeout=settings.media_download_timeout_seconds) as client:
        for room in missing_group_rooms[:20]:
            room_id = str(room["room_id"])
            group_info = None
            try:
                payload = await OneBotRPCService.call_action(robot_id, "get_group_info", {"group_id": int(room_id), "no_cache": False})
                if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
                    group_info = payload["data"]
            except (LookupError, asyncio.TimeoutError, ValueError):
                group_info = None
            await RoomProfileService.cache_qq_group_profile(
                db,
                room_id=room_id,
                platform="qq",
                group_info=group_info,
                http_client=client,
                storage_root=settings.storage_root,
                public_prefix=settings.public_storage_prefix,
                max_bytes=settings.media_max_bytes,
            )
            changed = True
        for room in missing_private_rooms[:20]:
            room_id = str(room["room_id"])
            await UserProfileService.cache_qq_user_profile(
                db,
                user_id=room_id,
                platform="qq",
                display_name=None,
                http_client=client,
                storage_root=settings.storage_root,
                public_prefix=settings.public_storage_prefix,
                max_bytes=settings.media_max_bytes,
            )
            changed = True
    return changed


@router.get("/messages", response_model=list[MessageResponse])
async def list_messages(
    robot_id: str = Query(..., min_length=1),
    room_id: str = Query(..., min_length=1),
    before_timestamp: int | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db_session),
) -> list[MessageResponse]:
    messages = await QueryService.list_messages(
        db,
        robot_id=robot_id,
        room_id=room_id,
        before_timestamp=before_timestamp,
        limit=limit,
    )
    return [MessageResponse.model_validate(message) for message in messages]


@router.post("/messages", response_model=MessageIngestResponse, status_code=status.HTTP_201_CREATED)
async def ingest_message(
    payload: MessageIngestRequest,
    db: AsyncSession = Depends(get_db_session),
) -> MessageIngestResponse:
    msg_hash = await MessageService.process_incoming_message(
        db,
        robot_id=payload.robot_id,
        platform=payload.platform,
        msg_data={
            "message_id": payload.message_id,
            "room_id": payload.room_id,
            "message_type": payload.message_type,
            "sender_id": payload.sender_id,
            "nickname": payload.nickname,
            "raw_message": payload.raw_message,
            "local_message": payload.local_message or payload.raw_message,
            "timestamp": payload.timestamp,
        },
    )
    display_name = payload.nickname if payload.sender_id == payload.robot_id else None
    await BotProfileService.upsert_bot_profile(
        db,
        robot_id=payload.robot_id,
        platform=payload.platform,
        display_name=display_name,
    )
    return MessageIngestResponse(msg_hash=msg_hash)


@router.post("/wechat/events", response_model=MessageIngestResponse, status_code=status.HTTP_201_CREATED)
async def ingest_wechat_event(
    payload: dict[str, Any],
    db: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> MessageIngestResponse:
    normalized = normalize_wechat_event(payload)
    if normalized is None:
        raise HTTPException(status_code=422, detail="unsupported wechat message event")

    async with httpx.AsyncClient(timeout=settings.media_download_timeout_seconds) as client:
        msg_hash = await MessageService.process_incoming_message(
            db,
            robot_id=normalized.robot_id,
            platform=normalized.platform,
            msg_data=normalized.msg_data,
            media_http_client=client,
            media_storage_root=settings.storage_root,
            media_public_prefix=settings.public_storage_prefix,
        )

    await BotProfileService.upsert_bot_profile(
        db,
        robot_id=normalized.robot_id,
        platform=normalized.platform,
        display_name=payload.get("robot_name") or payload.get("account_name"),
    )
    sender_avatar_path = await ProfilePlaceholderService.save_placeholder_avatar(
        db,
        profile_type="user",
        profile_id=normalized.msg_data["sender_id"],
        display_name=normalized.msg_data.get("nickname"),
        storage_root=settings.storage_root,
        public_prefix=settings.public_storage_prefix,
    )
    await UserProfileService.upsert_user_profile(
        db,
        user_id=normalized.msg_data["sender_id"],
        platform=normalized.platform,
        display_name=normalized.msg_data.get("nickname"),
        avatar_path=sender_avatar_path,
    )
    if normalized.msg_data["message_type"] == "group":
        room_avatar_path = await ProfilePlaceholderService.save_placeholder_avatar(
            db,
            profile_type="room",
            profile_id=normalized.msg_data["room_id"],
            display_name=payload.get("room_name") or payload.get("group_name"),
            storage_root=settings.storage_root,
            public_prefix=settings.public_storage_prefix,
        )
        await RoomProfileService.upsert_room_profile(
            db,
            room_id=normalized.msg_data["room_id"],
            platform=normalized.platform,
            display_name=payload.get("room_name") or payload.get("group_name"),
            avatar_path=room_avatar_path,
        )
    return MessageIngestResponse(msg_hash=msg_hash)


@router.get("/search", response_model=list[MessageResponse])
async def search_messages(
    robot_id: str = Query(..., min_length=1),
    keyword: str | None = Query(default=None),
    room_id: str | None = Query(default=None),
    sender_id: str | None = Query(default=None),
    start_timestamp: int | None = Query(default=None),
    end_timestamp: int | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db_session),
) -> list[MessageResponse]:
    messages = await QueryService.search_messages(
        db,
        robot_id=robot_id,
        keyword=keyword,
        room_id=room_id,
        sender_id=sender_id,
        start_timestamp=start_timestamp,
        end_timestamp=end_timestamp,
        limit=limit,
    )
    return [MessageResponse.model_validate(message) for message in messages]


@router.post("/media/backfill", response_model=MediaBackfillResponse)
async def backfill_media(
    request: Request,
    limit: int = Query(default=100, ge=1, le=1000),
    dry_run: bool = Query(default=False),
    finalize_unavailable: bool = Query(default=False),
    failure_limit: int = Query(default=20, ge=0, le=200),
    db: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> MediaBackfillResponse:
    action = "media.backfill"
    if not dry_run:
        _enforce_high_risk_rate_limit(request, action, settings)

    async def load_forward(robot_id: str, forward_id: str) -> dict:
        return await OneBotRPCService.call_action(robot_id, "get_forward_msg", {"id": forward_id})

    try:
        async with httpx.AsyncClient(timeout=settings.media_download_timeout_seconds) as client:
            report = await MediaBackfillService.backfill_historical_media(
                db,
                limit=limit,
                dry_run=dry_run,
                failure_limit=failure_limit,
                http_client=client,
                storage_root=settings.storage_root,
                public_prefix=settings.public_storage_prefix,
                max_bytes=settings.media_max_bytes,
                forward_payload_loader=load_forward,
                finalize_unavailable=finalize_unavailable,
            )
    except Exception as exc:
        if not dry_run:
            await _audit(db, request, action=action, status_="failed", detail={"error": str(exc), "limit": limit})
        raise
    if not dry_run:
        await _audit(db, request, action=action, status_="success", detail={"limit": limit, "updated": report.updated, "failed": report.failed})
    return MediaBackfillResponse(
        scanned=report.scanned,
        candidates=report.candidates,
        updated=report.updated,
        unchanged=report.unchanged,
        failed=report.failed,
        media_failed=report.media_failed,
        forward_failed=report.forward_failed,
        failures=[failure.__dict__ for failure in report.failures],
    )


@router.get("/offline/audit", response_model=OfflineAuditResponse)
async def audit_offline_readiness(
    robot_id: str | None = Query(default=None, min_length=1),
    room_id: str | None = Query(default=None, min_length=1),
    limit: int = Query(default=5000, ge=1, le=50000),
    issue_limit: int = Query(default=100, ge=0, le=1000),
    db: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> OfflineAuditResponse:
    report = await OfflineAuditService.audit_offline_readiness(
        db,
        robot_id=robot_id,
        room_id=room_id,
        limit=limit,
        issue_limit=issue_limit,
        storage_root=settings.storage_root,
        public_storage_prefix=settings.public_storage_prefix,
    )
    return OfflineAuditResponse(
        offline_ready=report.offline_ready,
        messages_scanned=report.messages_scanned,
        media_assets_checked=report.media_assets_checked,
        profile_avatars_checked=report.profile_avatars_checked,
        remote_media_urls=report.remote_media_urls,
        uncached_card_pages=report.uncached_card_pages,
        uncached_forwards=report.uncached_forwards,
        missing_profile_avatars=report.missing_profile_avatars,
        missing_media_assets=report.missing_media_assets,
        missing_media_files=report.missing_media_files,
        issues=[issue.__dict__ for issue in report.issues],
    )


@router.post("/offline/repair", response_model=OfflineRepairResponse)
async def repair_offline_media_integrity(
    request: Request,
    limit: int = Query(default=50000, ge=1, le=50000),
    db: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> OfflineRepairResponse:
    action = "offline.repair"
    _enforce_high_risk_rate_limit(request, action, settings)
    try:
        report = await OfflineRepairService.repair_local_media_integrity(
            db,
            limit=limit,
            storage_root=settings.storage_root,
            public_storage_prefix=settings.public_storage_prefix,
        )
    except Exception as exc:
        await _audit(db, request, action=action, status_="failed", detail={"error": str(exc), "limit": limit})
        raise
    await _audit(
        db,
        request,
        action=action,
        status_="success",
        detail={
            "limit": limit,
            "repaired_media_assets": report.repaired_media_assets,
            "repaired_media_files": report.repaired_media_files,
            "repaired_profile_avatars": report.repaired_profile_avatars,
        },
    )
    return OfflineRepairResponse(
        scanned_messages=report.scanned_messages,
        repaired_media_assets=report.repaired_media_assets,
        repaired_media_files=report.repaired_media_files,
        repaired_file_sizes=report.repaired_file_sizes,
        repaired_profile_avatars=report.repaired_profile_avatars,
        repaired_paths=report.repaired_paths,
    )


@router.get("/forward")
async def get_forward_message(
    robot_id: str = Query(..., min_length=1),
    forward_id: str = Query(..., min_length=1),
    db: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> dict:
    try:
        payload = await OneBotRPCService.call_action(robot_id, "get_forward_msg", {"id": forward_id})
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    except asyncio.TimeoutError as exc:
        raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail="onebot action timed out") from exc
    async with httpx.AsyncClient(timeout=settings.media_download_timeout_seconds) as client:
        localized = await MediaService.localize_onebot_payload(
            db,
            payload,
            http_client=client,
            storage_root=settings.storage_root,
            public_prefix=settings.public_storage_prefix,
            max_bytes=settings.media_max_bytes,
        )
    local_path = await MessageService.save_media_asset(
        db,
        file_content=json.dumps(localized, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        file_type="forward",
        ext="json",
        storage_root=settings.storage_root,
        public_prefix=settings.public_storage_prefix,
    )
    await _attach_local_forward_payload(db, robot_id=robot_id, forward_id=forward_id, local_path=local_path)
    return localized


async def _attach_local_forward_payload(db: AsyncSession, robot_id: str, forward_id: str, local_path: str) -> None:
    result = await db.execute(
        select(Message)
        .join(RobotMessage, RobotMessage.msg_hash == Message.msg_hash)
        .where(RobotMessage.robot_id == robot_id, Message.local_message.like(f"%{forward_id}%"))
    )
    for message in result.scalars().unique().all():
        message.local_message = _rewrite_forward_segment_with_local_path(message.local_message, forward_id, local_path)
    await db.commit()


def _rewrite_forward_segment_with_local_path(local_message: str, forward_id: str, local_path: str) -> str:
    pattern = r"\[CQ:forward,(?P<params>[^\]]+)\]"

    def replace(match: re.Match[str]) -> str:
        params = _parse_cq_params(match.group("params"))
        if params.get("id") != forward_id:
            return match.group(0)
        params["local"] = local_path
        return _build_cq_segment("forward", params)

    return re.sub(pattern, replace, local_message)


@router.get("/export")
async def export_data(
    robot_id: str | None = Query(default=None, min_length=1),
    room_id: str | None = Query(default=None, min_length=1),
    start_timestamp: int | None = Query(default=None),
    end_timestamp: int | None = Query(default=None),
    db: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
):
    return await BackupService.export_package(
        db,
        robot_id=robot_id,
        room_id=room_id,
        start_timestamp=start_timestamp,
        end_timestamp=end_timestamp,
        storage_root=settings.storage_root,
        public_storage_prefix=settings.public_storage_prefix,
        max_media_bytes=settings.media_max_bytes,
        system_id=settings.system_instance_id,
        signing_key=settings.app_secret_key,
    )


@router.post("/import/validate", response_model=ImportValidationResponse)
async def validate_import_data(
    package: dict,
    db: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> ImportValidationResponse:
    report = await BackupService.preview_import_package(
        db,
        package,
        storage_root=settings.storage_root,
        public_storage_prefix=settings.public_storage_prefix,
        signing_key=settings.app_secret_key,
    )
    return ImportValidationResponse(**report)


@router.post("/import", response_model=ImportResultResponse)
async def import_data(
    package: dict,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> ImportResultResponse:
    action = "import.run"
    _enforce_high_risk_rate_limit(request, action, settings)
    try:
        result = await BackupService.import_package(
            db,
            package,
            storage_root=settings.storage_root,
            public_storage_prefix=settings.public_storage_prefix,
            signing_key=settings.app_secret_key,
        )
    except ValueError as exc:
        BackupService.write_failure_log(settings.backup_root, event="import", error=str(exc), context={"schema": (package.get("manifest") or {}).get("schema")})
        await _audit(db, request, action=action, status_="failed", detail={"error": str(exc), "schema": (package.get("manifest") or {}).get("schema")})
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _audit(db, request, action=action, status_="success", detail={"messages": result["messages"], "robot_messages": result["robot_messages"], "media_assets": result["media_assets"]})
    return ImportResultResponse(**result)
