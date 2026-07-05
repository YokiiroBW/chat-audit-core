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
from app.database import LIGHTWEIGHT_MIGRATIONS, get_db_session
from app.models import Message, RobotMessage, SchemaMigration
from app.schemas import (
    AdapterCreateRequest,
    AdapterResponse,
    AdapterUpdateRequest,
    AdminTokenCreateRequest,
    AdminTokenRotateResponse,
    AdminTokenResponse,
    AdminUserCreateRequest,
    AdminUserResponse,
    AuditLogResponse,
    AuthLoginRequest,
    AuthLoginResponse,
    AuthMeResponse,
    BackupRunResponse,
    BackupSettingsUpdateRequest,
    BackupStatusResponse,
    BotProfileResponse,
    DashboardResponse,
    ImportResultResponse,
    ImportValidationResponse,
    MediaBackfillResponse,
    MessageIngestRequest,
    MessageIngestResponse,
    MessageResponse,
    MigrationStatusResponse,
    OfflineAuditResponse,
    OfflineRepairResponse,
    RoomResponse,
    RuntimeStatusResponse,
)
from app.services.adapter_service import AdapterService
from app.services.admin_token_service import AdminTokenService, VALID_ADMIN_ROLES
from app.services.audit_log_service import AuditLogService
from app.services.bot_profile_service import BotProfileService
from app.services.backup_service import BackupService
from app.services.backup_config_service import BackupConfigService, EffectiveBackupConfig
from app.services.admin_user_service import AdminUserService
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
from app.services.runtime_service import RuntimeService
from app.services.user_profile_service import UserProfileService


_RATE_LIMIT_BUCKETS: dict[tuple[str, str], list[float]] = {}
_VALID_ADMIN_ROLES = VALID_ADMIN_ROLES


def _extract_bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token


def _normalize_admin_role(role: Any) -> str:
    normalized = str(role or "viewer").strip().lower()
    return normalized if normalized in _VALID_ADMIN_ROLES else "viewer"


def _admin_token_records(settings: Settings) -> dict[str, dict[str, str]]:
    records: dict[str, dict[str, str]] = {}
    legacy_token = settings.admin_api_token.strip()
    if legacy_token:
        records[legacy_token] = {"role": "admin", "name": "admin-token"}

    raw_tokens = settings.admin_api_tokens.strip()
    if not raw_tokens:
        return records

    try:
        parsed = json.loads(raw_tokens)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="ADMIN_API_TOKENS must be valid JSON") from exc

    if isinstance(parsed, list):
        for index, item in enumerate(parsed):
            if not isinstance(item, dict):
                continue
            token = str(item.get("token") or "").strip()
            if not token:
                continue
            records[token] = {
                "role": _normalize_admin_role(item.get("role")),
                "name": str(item.get("name") or f"token-{index + 1}"),
            }
    elif isinstance(parsed, dict):
        for token, role in parsed.items():
            token_value = str(token).strip()
            if not token_value:
                continue
            records[token_value] = {
                "role": _normalize_admin_role(role),
                "name": f"{_normalize_admin_role(role)}-token",
            }
    else:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="ADMIN_API_TOKENS must be a JSON object or array")

    return records


async def require_admin_api_token(
    request: Request,
    settings: Settings = Depends(get_settings),
    db: AsyncSession = Depends(get_db_session),
) -> None:
    token_records = _admin_token_records(settings)
    if not token_records:
        request.state.admin_role = "admin"
        request.state.admin_actor = "development-open"
        return

    header_token = request.headers.get("x-admin-token")
    bearer_token = _extract_bearer_token(request.headers.get("authorization"))
    provided_token = header_token or bearer_token
    matched = token_records.get(header_token or "") or token_records.get(bearer_token or "")
    if matched:
        request.state.admin_role = matched["role"]
        request.state.admin_actor = matched["name"]
        return
    if provided_token:
        managed_match = await AdminTokenService.match_token(db, provided_token)
        if managed_match is not None:
            request.state.admin_role = managed_match.role
            request.state.admin_actor = managed_match.actor
            request.state.admin_token_id = managed_match.token_id
            return
        session_match = await AdminUserService.match_session(db, provided_token)
        if session_match is not None:
            request.state.admin_role = session_match.role
            request.state.admin_actor = session_match.actor
            request.state.admin_user_id = session_match.user_id
            request.state.admin_session_id = session_match.session_id
            request.state.admin_username = session_match.username
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


public_router = APIRouter()
router = APIRouter(dependencies=[Depends(require_admin_api_token)])


def _client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    return request.client.host if request.client else None


def _actor(request: Request) -> str:
    return str(getattr(request.state, "admin_actor", "development-open"))


def require_admin_role(*allowed_roles: str):
    allowed = {_normalize_admin_role(role) for role in allowed_roles}

    async def dependency(
        request: Request,
        db: AsyncSession = Depends(get_db_session),
    ) -> None:
        role = str(getattr(request.state, "admin_role", "viewer"))
        if role == "admin" or role in allowed:
            return
        await _audit(
            db,
            request,
            action="auth.forbidden",
            status_="failed",
            target=request.url.path,
            detail={"method": request.method, "role": role, "required_roles": sorted(allowed)},
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="insufficient admin role")

    return dependency


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


@public_router.post("/auth/login", response_model=AuthLoginResponse)
async def login_admin_user(
    payload: AuthLoginRequest,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
) -> AuthLoginResponse:
    user = await AdminUserService.authenticate(db, username=payload.username, password=payload.password)
    if user is None:
        await AuditLogService.record(
            db,
            action="auth.login",
            status="failed",
            actor=payload.username.strip().lower() or "anonymous",
            ip_address=_client_ip(request),
            target="admin_user",
            detail={"reason": "invalid_credentials"},
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid username or password")
    _session, token = await AdminUserService.create_session(db, user)
    await AuditLogService.record(
        db,
        action="auth.login",
        status="success",
        actor=f"db-user:{user.username}",
        ip_address=_client_ip(request),
        target=str(user.id),
        detail={"role": user.role},
    )
    return AuthLoginResponse(token=token, user=AdminUserResponse.model_validate(user))


@router.get("/auth/me", response_model=AuthMeResponse)
async def get_auth_identity(request: Request) -> AuthMeResponse:
    return AuthMeResponse(
        actor=_actor(request),
        role=str(getattr(request.state, "admin_role", "viewer")),
        user_id=getattr(request.state, "admin_user_id", None),
        session_id=getattr(request.state, "admin_session_id", None),
        username=getattr(request.state, "admin_username", None),
    )


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout_admin_user(
    request: Request,
    db: AsyncSession = Depends(get_db_session),
) -> Response:
    token = _extract_bearer_token(request.headers.get("authorization"))
    if token:
        await AdminUserService.revoke_session(db, token)
    await _audit(db, request, action="auth.logout", status_="success")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


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


def _backup_status_response(settings: Settings, backup_config: EffectiveBackupConfig) -> BackupStatusResponse:
    backup_root = settings.backup_root
    backups = sorted(backup_root.glob("auto-backup-*.json"), key=lambda path: (path.stat().st_mtime, path.name)) if backup_root.exists() else []
    latest = backups[-1].name if backups else None
    return BackupStatusResponse(
        enabled=backup_config.enabled,
        cron=backup_config.cron,
        keep_latest=backup_config.keep_latest,
        backup_root=str(backup_root),
        backups=len(backups),
        latest_backup=latest,
        config_source=backup_config.config_source,
        cron_source=backup_config.cron_source,
        keep_latest_source=backup_config.keep_latest_source,
    )


@router.get("/backup/status", response_model=BackupStatusResponse)
async def get_backup_status(
    db: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> BackupStatusResponse:
    backup_config = await BackupConfigService.get_effective_config(db, settings)
    return _backup_status_response(settings, backup_config)


@router.patch("/backup/settings", response_model=BackupStatusResponse)
async def update_backup_settings(
    payload: BackupSettingsUpdateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
    _: None = Depends(require_admin_role("operator", "admin")),
) -> BackupStatusResponse:
    action = "backup.settings.update"
    _enforce_high_risk_rate_limit(request, action, settings)
    before = await BackupConfigService.get_effective_config(db, settings)
    try:
        backup_config = await BackupConfigService.update_config(
            db,
            settings,
            cron=payload.cron,
            keep_latest=payload.keep_latest,
            reset_to_env=payload.reset_to_env,
        )
    except ValueError as exc:
        await _audit(db, request, action=action, status_="failed", detail={"error": str(exc)})
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await _audit(
        db,
        request,
        action=action,
        status_="success",
        detail={
            "before": {"cron": before.cron, "keep_latest": before.keep_latest, "source": before.config_source},
            "after": {"cron": backup_config.cron, "keep_latest": backup_config.keep_latest, "source": backup_config.config_source},
            "reset_to_env": payload.reset_to_env,
        },
    )
    return _backup_status_response(settings, backup_config)


@router.post("/backup/run", response_model=BackupRunResponse)
async def run_backup_now(
    request: Request,
    db: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
    _: None = Depends(require_admin_role("operator", "admin")),
) -> BackupRunResponse:
    action = "backup.run"
    _enforce_high_risk_rate_limit(request, action, settings)
    backup_config = await BackupConfigService.get_effective_config(db, settings)
    try:
        path = await BackupService.write_auto_backup_file(
            db,
            backup_root=settings.backup_root,
            storage_root=settings.storage_root,
            public_storage_prefix=settings.public_storage_prefix,
            max_media_bytes=settings.media_max_bytes,
            keep_latest=backup_config.keep_latest,
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


@router.get("/admin/tokens", response_model=list[AdminTokenResponse])
async def list_admin_tokens(
    db: AsyncSession = Depends(get_db_session),
    _: None = Depends(require_admin_role("admin")),
) -> list[AdminTokenResponse]:
    tokens = await AdminTokenService.list_tokens(db)
    return [AdminTokenResponse.model_validate(token) for token in tokens]


@router.post("/admin/tokens", response_model=AdminTokenResponse, status_code=status.HTTP_201_CREATED)
async def create_admin_token(
    payload: AdminTokenCreateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
    _: None = Depends(require_admin_role("admin")),
) -> AdminTokenResponse:
    action = "admin_token.create"
    _enforce_high_risk_rate_limit(request, action, settings)
    record, token = await AdminTokenService.create_token(db, name=payload.name, role=payload.role)
    await _audit(db, request, action=action, status_="success", target=str(record.id), detail={"name": record.name, "role": record.role})
    response = AdminTokenResponse.model_validate(record)
    response.token = token
    return response


@router.delete("/admin/tokens/{token_id}", response_model=AdminTokenResponse)
async def revoke_admin_token(
    token_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
    _: None = Depends(require_admin_role("admin")),
) -> AdminTokenResponse:
    action = "admin_token.revoke"
    _enforce_high_risk_rate_limit(request, action, settings)
    record = await AdminTokenService.revoke_token(db, token_id)
    if record is None:
        await _audit(db, request, action=action, status_="failed", target=str(token_id), detail={"reason": "not_found"})
        raise HTTPException(status_code=404, detail="admin token not found")
    await _audit(db, request, action=action, status_="success", target=str(record.id), detail={"name": record.name, "role": record.role})
    return AdminTokenResponse.model_validate(record)


@router.post("/admin/tokens/{token_id}/rotate", response_model=AdminTokenRotateResponse)
async def rotate_admin_token(
    token_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
    _: None = Depends(require_admin_role("admin")),
) -> AdminTokenRotateResponse:
    action = "admin_token.rotate"
    _enforce_high_risk_rate_limit(request, action, settings)
    rotated = await AdminTokenService.rotate_token(db, token_id)
    if rotated is None:
        await _audit(db, request, action=action, status_="failed", target=str(token_id), detail={"reason": "not_found"})
        raise HTTPException(status_code=404, detail="admin token not found")
    record, token = rotated
    await _audit(db, request, action=action, status_="success", target=str(record.id), detail={"name": record.name, "role": record.role})
    response = AdminTokenRotateResponse.model_validate(record)
    response.token = token
    return response


@router.get("/admin/users", response_model=list[AdminUserResponse])
async def list_admin_users(
    db: AsyncSession = Depends(get_db_session),
    _: None = Depends(require_admin_role("admin")),
) -> list[AdminUserResponse]:
    users = await AdminUserService.list_users(db)
    return [AdminUserResponse.model_validate(user) for user in users]


@router.post("/admin/users", response_model=AdminUserResponse, status_code=status.HTTP_201_CREATED)
async def create_admin_user(
    payload: AdminUserCreateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
    _: None = Depends(require_admin_role("admin")),
) -> AdminUserResponse:
    action = "admin_user.create"
    _enforce_high_risk_rate_limit(request, action, settings)
    try:
        user = await AdminUserService.create_user(
            db,
            username=payload.username,
            password=payload.password,
            role=payload.role,
            display_name=payload.display_name,
        )
    except ValueError as exc:
        await _audit(db, request, action=action, status_="failed", target=payload.username, detail={"error": str(exc)})
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await _audit(db, request, action=action, status_="success", target=str(user.id), detail={"username": user.username, "role": user.role})
    return AdminUserResponse.model_validate(user)


@router.delete("/admin/users/{user_id}", response_model=AdminUserResponse)
async def revoke_admin_user(
    user_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
    _: None = Depends(require_admin_role("admin")),
) -> AdminUserResponse:
    action = "admin_user.revoke"
    _enforce_high_risk_rate_limit(request, action, settings)
    user = await AdminUserService.revoke_user(db, user_id)
    if user is None:
        await _audit(db, request, action=action, status_="failed", target=str(user_id), detail={"reason": "not_found"})
        raise HTTPException(status_code=404, detail="admin user not found")
    await _audit(db, request, action=action, status_="success", target=str(user.id), detail={"username": user.username, "role": user.role})
    return AdminUserResponse.model_validate(user)


@router.get("/system/migrations", response_model=list[MigrationStatusResponse])
async def list_migration_status(db: AsyncSession = Depends(get_db_session)) -> list[MigrationStatusResponse]:
    result = await db.execute(select(SchemaMigration))
    applied = {migration.version: migration for migration in result.scalars().all()}
    return [
        MigrationStatusResponse(
            version=version,
            description=description,
            applied=version in applied,
            applied_at=applied[version].applied_at if version in applied else None,
        )
        for version, description in LIGHTWEIGHT_MIGRATIONS.items()
    ]


@router.get("/system/runtime", response_model=RuntimeStatusResponse)
async def get_runtime_status(settings: Settings = Depends(get_settings)) -> RuntimeStatusResponse:
    return RuntimeStatusResponse(**RuntimeService.ffmpeg_status(settings))


@router.post("/adapters", response_model=AdapterResponse, status_code=status.HTTP_201_CREATED)
async def create_adapter(
    payload: AdapterCreateRequest,
    db: AsyncSession = Depends(get_db_session),
    _: None = Depends(require_admin_role("operator", "admin")),
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
    _: None = Depends(require_admin_role("operator", "admin")),
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
    _: None = Depends(require_admin_role("admin")),
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
    _: None = Depends(require_admin_role("operator", "admin")),
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
    _: None = Depends(require_admin_role("operator", "admin")),
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
    _: None = Depends(require_admin_role("operator", "admin")),
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
    _: None = Depends(require_admin_role("operator", "admin")),
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
    _: None = Depends(require_admin_role("admin")),
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
