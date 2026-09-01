import asyncio
from email.parser import BytesParser
from email.policy import default as email_policy
from functools import lru_cache, partial
import hashlib
import ipaddress
import json
import logging
from pathlib import Path
import re
import time
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.backup.jobs import (
    BackupAlreadyRunningError,
    active_backup_job,
    enqueue_backup_job,
    get_backup_job,
    list_completed_backups,
)
from app.backup.worker import worker_is_healthy
from app.conversation_identity import AmbiguousConversationError
from app.database import LIGHTWEIGHT_MIGRATIONS, get_db_session
from app.metrics import metrics_registry
from app.models import AdminUser, Message, RobotMessage, SchemaMigration
from app.storage_paths import is_local_storage_path
from app.schemas import (
    AdapterCreateRequest,
    AdapterResponse,
    AdapterUpdateRequest,
    AdminTokenCreateRequest,
    AdminTokenRotateResponse,
    AdminTokenResponse,
    AdminUserCreateRequest,
    AdminUserPasswordResetRequest,
    AdminUserResponse,
    AdminSessionResponse,
    AuditLogResponse,
    AuthLoginRequest,
    AuthLoginResponse,
    AuthMeResponse,
    BackupRunResponse,
    BackupSettingsUpdateRequest,
    BackupStatusResponse,
    BotProfileResponse,
    CaptureTargetPolicyResponse,
    CaptureTargetPolicyUpdateRequest,
    CaptureTargetSettingResponse,
    DashboardResponse,
    ExternalMediaUploadResponse,
    ImportResultResponse,
    ImportValidationResponse,
    ImportBatchCreateRequest,
    ImportBatchFinishRequest,
    ImportBatchMessagesRequest,
    ImportBatchMessagesResponse,
    ImportBatchResponse,
    ImportSourceCreateRequest,
    ImportSourceResponse,
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
from app.time_utils import format_utc_z
from app.services.adapter_service import AdapterService
from app.services.admin_token_service import AdminTokenService, VALID_ADMIN_ROLES
from app.services.audit_log_service import AuditLogService
from app.services.bot_profile_service import BotProfileService
from app.services.backup_service import BackupService
from app.services.backup_config_service import BackupConfigService, EffectiveBackupConfig
from app.services.capture_policy_service import CapturePolicyService
from app.services.admin_user_service import AdminUserService
from app.services.dashboard_service import DashboardService
from app.services.media_backfill_service import MediaBackfillService
from app.services.media_service import MediaService, _build_cq_segment, _parse_cq_params
from app.services.message_service import MessageService
from app.services.import_service import (
    ImportConflictError,
    ImportNotFoundError,
    ImportService,
    ImportServiceError,
    ImportStateError,
    import_batch_to_dict,
    import_source_to_dict,
)
from app.services.offline_audit_service import OfflineAuditService
from app.services.offline_repair_service import OfflineRepairService
from app.services.onebot_rpc_service import OneBotRPCService
from app.services.profile_placeholder_service import ProfilePlaceholderService
from app.services.query_service import QueryService
from app.services.room_profile_service import RoomProfileService
from app.services.runtime_service import RuntimeService
from app.services.user_profile_service import UserProfileService


_RATE_LIMIT_BUCKETS: dict[tuple[str, str], list[float]] = {}
# Above this many tracked buckets, drop the ones whose window has fully expired.
_RATE_LIMIT_BUCKET_LIMIT = 1024
_VALID_ADMIN_ROLES = VALID_ADMIN_ROLES
_EXTERNAL_MEDIA_TYPES = {"image", "voice", "video", "file"}
logger = logging.getLogger(__name__)
_EXTERNAL_MEDIA_ALIASES = {
    "audio": "voice",
    "record": "voice",
    "attachment": "file",
}


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
    cookie_token = request.cookies.get(settings.auth_session_cookie_name)
    provided_token = header_token or bearer_token or cookie_token
    matched = (
        token_records.get(header_token or "")
        or token_records.get(bearer_token or "")
        or token_records.get(cookie_token or "")
    )
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
media_router = APIRouter(dependencies=[Depends(require_admin_api_token)])


@lru_cache(maxsize=8)
def _trusted_proxy_networks(raw: str) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    networks = []
    for item in raw.split(","):
        candidate = item.strip()
        if not candidate:
            continue
        try:
            networks.append(ipaddress.ip_network(candidate, strict=False))
        except ValueError:
            logger.warning("Ignoring unparsable trusted proxy entry", extra={"entry": candidate})
    return tuple(networks)


def _client_ip(request: Request, settings: Settings | None = None) -> str | None:
    """The caller's address, trusting ``X-Forwarded-For`` only behind a proxy.

    The header is caller-supplied. Trusting it unconditionally let anyone mint a
    fresh rate-limit bucket per request and write whatever address they liked
    into the audit log, so it is only read when the immediate peer is a
    configured proxy. With TRUSTED_PROXY_IPS unset, which is the default, the
    header is ignored entirely.
    """
    peer = request.client.host if request.client else None
    networks = _trusted_proxy_networks(str(getattr(settings or get_settings(), "trusted_proxy_ips", "") or ""))
    if not networks or peer is None:
        return peer
    try:
        peer_address = ipaddress.ip_address(peer)
    except ValueError:
        return peer
    if not any(peer_address in network for network in networks):
        return peer
    # Walk right to left: our own proxies appended the rightmost entries, so the
    # first address that is not one of them is the real caller. Anything further
    # left was supplied by that caller and cannot be trusted.
    for candidate in reversed([item.strip() for item in (request.headers.get("x-forwarded-for") or "").split(",")]):
        if not candidate:
            continue
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if any(address in network for network in networks):
            continue
        return candidate
    return peer


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
    key = (_client_ip(request, settings) or "unknown", action)
    bucket = [timestamp for timestamp in _RATE_LIMIT_BUCKETS.get(key, []) if now - timestamp < 60]
    if len(bucket) >= limit:
        _RATE_LIMIT_BUCKETS[key] = bucket
        metrics_registry.record_rate_limit_exceeded(action=action, actor=_actor(request))
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Rate limit exceeded for {action}: max {limit} requests per minute",
            headers={"Retry-After": "60"},
        )
    bucket.append(now)
    _RATE_LIMIT_BUCKETS[key] = bucket
    # Every distinct caller and action leaves an entry behind for good
    # otherwise, so the dictionary only ever grew.
    if len(_RATE_LIMIT_BUCKETS) > _RATE_LIMIT_BUCKET_LIMIT:
        for stale_key, timestamps in list(_RATE_LIMIT_BUCKETS.items()):
            if stale_key != key and all(now - timestamp >= 60 for timestamp in timestamps):
                del _RATE_LIMIT_BUCKETS[stale_key]


def _normalize_external_media_type(value: Any) -> str:
    normalized = str(value or "file").strip().lower()
    return _EXTERNAL_MEDIA_ALIASES.get(normalized, normalized)


def _default_external_media_ext(media_type: str) -> str:
    return {"image": "jpg", "voice": "silk", "video": "mp4", "file": "bin"}[media_type]


def _external_media_ext(file_name: str | None, media_type: str) -> str:
    if file_name:
        suffix = Path(file_name).suffix.lstrip(".").lower()
        if suffix:
            return suffix
    return _default_external_media_ext(media_type)


def _parse_external_multipart(content_type: str, body: bytes) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    if "multipart/form-data" not in content_type.lower():
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="multipart/form-data is required")
    message = BytesParser(policy=email_policy).parsebytes(
        b"Content-Type: " + content_type.encode("utf-8") + b"\r\nMIME-Version: 1.0\r\n\r\n" + body
    )
    if not message.is_multipart():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid multipart payload")

    fields: dict[str, str] = {}
    files: dict[str, dict[str, Any]] = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        payload = part.get_payload(decode=True) or b""
        filename = part.get_filename()
        if filename is not None or name == "file":
            files[name] = {
                "content": payload,
                "file_name": filename or None,
                "content_type": part.get_content_type(),
            }
            continue
        charset = part.get_content_charset() or "utf-8"
        fields[name] = payload.decode(charset, errors="replace")
    return fields, files


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


def _set_auth_session_cookie(response: Response, settings: Settings, token: str) -> None:
    response.set_cookie(
        settings.auth_session_cookie_name,
        token,
        httponly=True,
        secure=settings.auth_session_cookie_secure,
        samesite="strict",
        max_age=86400 * 7,
        path="/",
    )


def _clear_auth_session_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        settings.auth_session_cookie_name,
        secure=settings.auth_session_cookie_secure,
        samesite="strict",
        path="/",
    )


def _storage_file_path(storage_root: Path, file_name: str) -> Path | None:
    root = storage_root.resolve()
    candidate = (root / file_name).resolve()
    if candidate == root or not candidate.is_relative_to(root):
        return None
    return candidate


# Archived card snapshots are attacker-controlled markup: the URL comes from a
# QQ card message and the body is whatever that server returned. The console
# opens them with a plain link, so without this they render as a document in the
# console's own origin, with the session cookie attached.
_ARCHIVED_DOCUMENT_SUFFIXES = {".htm", ".html", ".mhtml", ".shtml", ".svg", ".xhtml", ".xml"}
# ``sandbox`` with no tokens drops the document into an opaque origin and
# disables scripts outright, which keeps a snapshot readable while making it
# unable to touch the console, its cookies or its API. The rest stops the
# snapshot from reaching back out to the network when someone views it.
_ARCHIVED_DOCUMENT_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
    "base-uri 'none'; form-action 'none'; sandbox"
)


@media_router.get("/media/{file_name:path}", include_in_schema=False)
@media_router.get("/static/storage/{file_name:path}", include_in_schema=False)
async def download_media_file(
    file_name: str,
    settings: Settings = Depends(get_settings),
) -> FileResponse:
    file_path = _storage_file_path(settings.storage_root, file_name)
    if file_path is None or not file_path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="media file not found")
    headers = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}
    if file_path.suffix.lower() in _ARCHIVED_DOCUMENT_SUFFIXES:
        headers["Content-Security-Policy"] = _ARCHIVED_DOCUMENT_CSP
        headers["Referrer-Policy"] = "no-referrer"
    return FileResponse(file_path, headers=headers)


@public_router.post("/auth/login", response_model=AuthLoginResponse)
async def login_admin_user(
    payload: AuthLoginRequest,
    request: Request,
    response: Response,
    settings: Settings = Depends(get_settings),
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
    _set_auth_session_cookie(response, settings, token)
    return AuthLoginResponse(token=token, user=AdminUserResponse.model_validate(user))


@router.post("/auth/session", status_code=status.HTTP_204_NO_CONTENT)
async def establish_browser_session(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> Response:
    token = (
        request.headers.get("x-admin-token")
        or _extract_bearer_token(request.headers.get("authorization"))
        or request.cookies.get(settings.auth_session_cookie_name)
    )
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing admin api token")
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    _set_auth_session_cookie(response, settings, token)
    return response


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
    settings: Settings = Depends(get_settings),
    db: AsyncSession = Depends(get_db_session),
) -> Response:
    token = (
        request.headers.get("x-admin-token")
        or _extract_bearer_token(request.headers.get("authorization"))
        or request.cookies.get(settings.auth_session_cookie_name)
    )
    if token:
        await AdminUserService.revoke_session(db, token)
    await _audit(db, request, action="auth.logout", status_="success")
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    _clear_auth_session_cookie(response, settings)
    return response


@router.get("/adapters", response_model=list[AdapterResponse])
async def list_adapters(db: AsyncSession = Depends(get_db_session)) -> list[AdapterResponse]:
    adapters = await QueryService.list_adapters(db)
    return [AdapterResponse.model_validate(adapter) for adapter in adapters]


@router.get("/bots", response_model=list[BotProfileResponse])
async def list_bots(
    db: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> list[BotProfileResponse]:
    profiles = await QueryService.list_bot_profiles(db)
    if await _hydrate_missing_bot_profiles(db, profiles=profiles, settings=settings):
        profiles = await QueryService.list_bot_profiles(db)
    return [BotProfileResponse.model_validate(profile) for profile in profiles]


# One page of a list request may not turn into an unbounded fan-out of outbound
# HTTP calls, and one unreachable avatar may not take the list down with it.
AVATAR_HYDRATION_LIMIT = 20


async def _hydrate_profiles(
    db: AsyncSession,
    entries: list[tuple[str, Any]],
    *,
    kind: str,
) -> bool:
    """Run each hydration, isolated and capped.

    Every caller had one of the two failure modes: /bots and /messages ran the
    whole page inside a single try, so one profile that could not be fetched
    returned 500 for the entire list, and the private branch of /rooms had no
    cap at all, so a page of new private chats meant one serial outbound request
    per chat while the caller waited.

    True means "reload before using the rows you passed in". That covers a
    successful hydration, and equally a failed one: the rollback that makes the
    session usable again also expires whatever the caller is holding, and
    serialising an expired ORM row raises rather than returning stale data.
    """
    touched = False
    for label, run in entries[:AVATAR_HYDRATION_LIMIT]:
        try:
            await run()
        except Exception:
            await db.rollback()
            logger.exception("Profile hydration failed", extra={"kind": kind, "target": label})
        touched = True
    return touched


async def _hydrate_missing_bot_profiles(db: AsyncSession, profiles: list[Any], settings: Settings) -> bool:
    missing_qq_avatars = [
        profile
        for profile in profiles
        if getattr(profile, "platform", "") == "qq"
        and str(getattr(profile, "id", "")).isdigit()
        and _needs_qq_avatar_refresh(getattr(profile, "avatar_path", None))
    ]
    if not missing_qq_avatars:
        return False

    async with httpx.AsyncClient(timeout=settings.media_download_timeout_seconds) as client:
        return await _hydrate_profiles(
            db,
            [
                (
                    str(profile.id),
                    partial(
                        UserProfileService.cache_qq_user_profile,
                        db,
                        user_id=str(profile.id),
                        platform="qq",
                        display_name=profile.display_name,
                        http_client=client,
                        storage_root=settings.storage_root,
                        public_prefix=settings.public_storage_prefix,
                        max_bytes=settings.media_max_bytes,
                    ),
                )
                for profile in missing_qq_avatars
            ],
            kind="bot_avatar",
        )


def _needs_qq_avatar_refresh(avatar_path: str | None) -> bool:
    if not avatar_path:
        return True
    # A cached avatar is one served from any prefix this deployment exposes.
    # Checking a single hard-coded prefix makes every already-cached avatar look
    # stale under a different `public_storage_prefix`, so each list request would
    # re-download the whole page of avatars and never converge.
    return (not is_local_storage_path(avatar_path)) or avatar_path.lower().endswith(".svg")


@router.get("/bots/{robot_id}/capture-targets", response_model=list[CaptureTargetSettingResponse])
async def list_capture_targets(
    robot_id: str,
    db: AsyncSession = Depends(get_db_session),
) -> list[CaptureTargetSettingResponse]:
    targets = await CapturePolicyService.list_target_settings(db, robot_id=robot_id)
    return [CaptureTargetSettingResponse(**target) for target in targets]


@router.put("/bots/{robot_id}/capture-policies/{target_type}/{target_id}", response_model=CaptureTargetPolicyResponse)
async def upsert_capture_policy(
    robot_id: str,
    target_type: str,
    target_id: str,
    payload: CaptureTargetPolicyUpdateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
    _: None = Depends(require_admin_role("operator", "admin")),
) -> CaptureTargetPolicyResponse:
    action = "capture_policy.upsert"
    _enforce_high_risk_rate_limit(request, action, settings)
    try:
        policy = await CapturePolicyService.upsert_policy(
            db,
            robot_id=robot_id,
            target_type=target_type,
            target_id=target_id,
            list_mode=payload.list_mode,
            capture_text=payload.capture_text,
            capture_image=payload.capture_image,
            capture_voice=payload.capture_voice,
            capture_video=payload.capture_video,
            capture_file=payload.capture_file,
        )
    except ValueError as exc:
        await _audit(db, request, action=action, status_="failed", target=f"{robot_id}:{target_type}:{target_id}", detail={"error": str(exc)})
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await _audit(
        db,
        request,
        action=action,
        status_="success",
        target=f"{robot_id}:{policy.target_type}:{policy.target_id}",
        detail={"list_mode": policy.list_mode},
    )
    return CaptureTargetPolicyResponse(**CapturePolicyService.policy_to_dict(policy))


@router.delete("/bots/{robot_id}/capture-policies/{target_type}/{target_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_capture_policy(
    robot_id: str,
    target_type: str,
    target_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
    _: None = Depends(require_admin_role("operator", "admin")),
) -> Response:
    action = "capture_policy.delete"
    _enforce_high_risk_rate_limit(request, action, settings)
    try:
        deleted = await CapturePolicyService.delete_policy(db, robot_id=robot_id, target_type=target_type, target_id=target_id)
    except ValueError as exc:
        await _audit(db, request, action=action, status_="failed", target=f"{robot_id}:{target_type}:{target_id}", detail={"error": str(exc)})
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if deleted:
        await _audit(db, request, action=action, status_="success", target=f"{robot_id}:{target_type}:{target_id}")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/dashboard", response_model=DashboardResponse)
async def get_dashboard_summary(
    db: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> DashboardResponse:
    return DashboardResponse(**await DashboardService.get_summary(db, backup_root=settings.backup_root))


def _backup_status_response(settings: Settings, backup_config: EffectiveBackupConfig) -> BackupStatusResponse:
    backup_root = settings.backup_root
    backups = list_completed_backups(backup_root)
    latest = backups[-1].name if backups else None
    cron_error: str | None = None
    next_run_at: str | None = None
    if backup_config.enabled:
        try:
            next_run_at = format_utc_z(BackupService.next_run_from_cron(backup_config.cron))
        except ValueError as exc:
            cron_error = str(exc)
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
        cron_error=cron_error,
        next_run_at=next_run_at,
        worker_healthy=worker_is_healthy(settings),
        active_job=active_backup_job(backup_root),
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
    if not worker_is_healthy(settings):
        detail = "isolated backup worker is unavailable"
        await _audit(db, request, action=action, status_="failed", detail={"error": detail})
        raise HTTPException(status_code=503, detail=detail)
    try:
        job = enqueue_backup_job(
            settings.backup_root,
            backup_type="manual",
            created_by="manual_api",
            keep_latest=backup_config.keep_latest,
        )
    except BackupAlreadyRunningError as exc:
        await _audit(
            db,
            request,
            action=action,
            status_="failed",
            detail={"error": str(exc), "active_job_id": exc.job.get("job_id")},
        )
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        await _audit(db, request, action=action, status_="failed", detail={"error": str(exc)})
        raise
    await _audit(db, request, action=action, status_="success", target=job["job_id"], detail={"state": "queued"})
    return BackupRunResponse(**job)


@router.get("/backup/jobs/{job_id}", response_model=BackupRunResponse)
async def get_backup_job_status(
    job_id: str,
    settings: Settings = Depends(get_settings),
) -> BackupRunResponse:
    job = get_backup_job(settings.backup_root, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="backup job not found")
    return BackupRunResponse(**job)


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


@router.get("/admin/sessions", response_model=list[AdminSessionResponse])
async def list_admin_sessions(
    db: AsyncSession = Depends(get_db_session),
    _: None = Depends(require_admin_role("admin")),
) -> list[AdminSessionResponse]:
    sessions = await AdminUserService.list_sessions(db)
    return [
        AdminSessionResponse(
            id=session.id,
            user_id=user.id,
            username=user.username,
            role=user.role,
            token_prefix=session.token_prefix,
            status=session.status,
            created_at=session.created_at,
            last_used_at=session.last_used_at,
            revoked_at=session.revoked_at,
        )
        for session, user in sessions
    ]


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


@router.post("/admin/users/{user_id}/password", response_model=AdminUserResponse)
async def reset_admin_user_password(
    user_id: int,
    payload: AdminUserPasswordResetRequest,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
    _: None = Depends(require_admin_role("admin")),
) -> AdminUserResponse:
    action = "admin_user.password_reset"
    _enforce_high_risk_rate_limit(request, action, settings)
    user = await AdminUserService.reset_password(db, user_id, payload.password)
    if user is None:
        await _audit(db, request, action=action, status_="failed", target=str(user_id), detail={"reason": "not_found"})
        raise HTTPException(status_code=404, detail="admin user not found")
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


@router.delete("/admin/sessions/{session_id}", response_model=AdminSessionResponse)
async def revoke_admin_session(
    session_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
    _: None = Depends(require_admin_role("admin")),
) -> AdminSessionResponse:
    action = "admin_session.revoke"
    _enforce_high_risk_rate_limit(request, action, settings)
    session = await AdminUserService.revoke_session_by_id(db, session_id)
    if session is None:
        await _audit(db, request, action=action, status_="failed", target=str(session_id), detail={"reason": "not_found"})
        raise HTTPException(status_code=404, detail="admin session not found")
    user = await db.get(AdminUser, session.user_id)
    username = user.username if user is not None else str(session.user_id)
    role = user.role if user is not None else "viewer"
    await _audit(db, request, action=action, status_="success", target=str(session.id), detail={"user_id": session.user_id})
    return AdminSessionResponse(
        id=session.id,
        user_id=session.user_id,
        username=username,
        role=role,
        token_prefix=session.token_prefix,
        status=session.status,
        created_at=session.created_at,
        last_used_at=session.last_used_at,
        revoked_at=session.revoked_at,
    )


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


@router.post(
    "/adapters",
    response_model=AdapterResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an adapter",
    description="Register a QQ/NapCat or custom adapter before it starts sending messages.",
    responses={409: {"description": "Adapter id already exists"}},
)
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
        and (not room.get("display_name") or _needs_qq_avatar_refresh(room.get("avatar_path")))
    ]
    missing_private_rooms = [
        room
        for room in rooms
        if room.get("message_type") == "private"
        and str(room.get("qq_number") or room.get("room_id") or "").isdigit()
        and (not room.get("display_name") or _needs_qq_avatar_refresh(room.get("avatar_path")))
    ]
    if not missing_group_rooms and not missing_private_rooms:
        return False

    changed = False
    async with httpx.AsyncClient(timeout=settings.media_download_timeout_seconds) as client:
        for room in missing_group_rooms[:AVATAR_HYDRATION_LIMIT]:
            room_id = str(room["room_id"])
            group_info = None
            try:
                payload = await OneBotRPCService.call_action(robot_id, "get_group_info", {"group_id": int(room_id), "no_cache": False})
                if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
                    group_info = payload["data"]
            except (LookupError, asyncio.TimeoutError, ValueError):
                group_info = None
            try:
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
            except Exception:
                await db.rollback()
                logger.exception("Room profile hydration failed", extra={"room_id": room_id, "platform": "qq"})
        # Capped like every other branch: a page full of new private chats used
        # to mean one serial outbound request per chat while the caller waited.
        for room in missing_private_rooms[:AVATAR_HYDRATION_LIMIT]:
            user_id = str(room.get("qq_number") or room["room_id"])
            try:
                await UserProfileService.cache_qq_user_profile(
                    db,
                    user_id=user_id,
                    platform="qq",
                    display_name=room.get("display_name"),
                    http_client=client,
                    storage_root=settings.storage_root,
                    public_prefix=settings.public_storage_prefix,
                    max_bytes=settings.media_max_bytes,
                )
                changed = True
            except Exception:
                await db.rollback()
                logger.exception("User profile hydration failed", extra={"user_id": user_id, "platform": "qq"})
    return changed


@router.get("/messages", response_model=list[MessageResponse])
async def list_messages(
    robot_id: str = Query(..., min_length=1),
    room_id: str = Query(..., min_length=1),
    message_type: str | None = Query(default=None, pattern="^(group|private)$"),
    before_timestamp: int | None = Query(default=None),
    before_source_sequence: int | None = Query(default=None),
    before_msg_hash: str | None = Query(default=None, min_length=1),
    around_message_id: str | None = Query(default=None, min_length=1),
    media_state: str | None = Query(default=None, pattern="^(all|any|complete|not_downloaded|missing|failed)$"),
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> list[MessageResponse]:
    try:
        messages = await QueryService.list_messages(
            db,
            robot_id=robot_id,
            room_id=room_id,
            message_type=message_type,
            before_timestamp=before_timestamp,
            before_source_sequence=before_source_sequence,
            before_msg_hash=before_msg_hash,
            around_message_id=around_message_id,
            media_state=media_state,
            limit=limit,
        )
    except AmbiguousConversationError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if await _hydrate_missing_message_sender_profiles(db, messages=messages, settings=settings):
        messages = await QueryService.list_messages(
            db,
            robot_id=robot_id,
            room_id=room_id,
            message_type=message_type,
            before_timestamp=before_timestamp,
            before_source_sequence=before_source_sequence,
            before_msg_hash=before_msg_hash,
            around_message_id=around_message_id,
            media_state=media_state,
            limit=limit,
        )
    return [MessageResponse.model_validate(message) for message in messages]


async def _hydrate_missing_message_sender_profiles(db: AsyncSession, messages: list[Message], settings: Settings) -> bool:
    missing_senders: dict[str, str | None] = {}
    for message in messages:
        sender_id = str(message.sender_id or "")
        if (
            message.platform == "qq"
            and sender_id.isdigit()
            and _needs_qq_avatar_refresh(getattr(message, "sender_avatar_path", None))
            and sender_id not in missing_senders
        ):
            missing_senders[sender_id] = getattr(message, "sender_display_name", None) or message.nickname

    if not missing_senders:
        return False

    async with httpx.AsyncClient(timeout=settings.media_download_timeout_seconds) as client:
        return await _hydrate_profiles(
            db,
            [
                (
                    sender_id,
                    partial(
                        UserProfileService.cache_qq_user_profile,
                        db,
                        user_id=sender_id,
                        platform="qq",
                        display_name=display_name,
                        http_client=client,
                        storage_root=settings.storage_root,
                        public_prefix=settings.public_storage_prefix,
                        max_bytes=settings.media_max_bytes,
                    ),
                )
                for sender_id, display_name in missing_senders.items()
            ],
            kind="message_sender_avatar",
        )


@router.post(
    "/messages",
    response_model=MessageIngestResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Ingest a normalized message",
    description="Receive a normalized message from an external collector. Capture policies may skip the message and return skipped=true.",
    responses={
        201: {"description": "Message accepted", "content": {"application/json": {"example": {"msg_hash": "sha256...", "skipped": False, "skip_reason": None}}}},
        401: {"description": "Missing or invalid admin token"},
        403: {"description": "Token role cannot write messages"},
    },
)
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
            "canonical_sender_id": payload.canonical_sender_id,
            "canonical_room_id": payload.canonical_room_id,
            "is_outgoing": payload.is_outgoing,
            "source_event_type": payload.source_event_type,
            "message_segments": payload.message_segments,
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
    return MessageIngestResponse(
        msg_hash=msg_hash,
        skipped=msg_hash is None,
        skip_reason="capture_policy" if msg_hash is None else None,
    )


def _raise_import_http_error(exc: ImportServiceError) -> None:
    if isinstance(exc, ImportNotFoundError):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if isinstance(exc, (ImportConflictError, ImportStateError)):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc


@router.post(
    "/import/sources",
    response_model=ImportSourceResponse,
    summary="Register or refresh a Collector import source",
)
async def upsert_import_source(
    payload: ImportSourceCreateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
    _: None = Depends(require_admin_role("operator", "admin")),
) -> ImportSourceResponse:
    try:
        source, created = await ImportService.upsert_source(db, payload)
    except ImportServiceError as exc:
        _raise_import_http_error(exc)
    await _audit(
        db,
        request,
        action="import.source.upsert",
        status_="success",
        target=source.id,
        detail={"created": created, "source_type": source.source_type, "platform": source.platform},
    )
    return ImportSourceResponse.model_validate(import_source_to_dict(source))


@router.post(
    "/import/batches",
    response_model=ImportBatchResponse,
    summary="Create or replay a Collector import batch",
)
async def create_import_batch(
    payload: ImportBatchCreateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
    _: None = Depends(require_admin_role("operator", "admin")),
) -> ImportBatchResponse:
    try:
        batch, created = await ImportService.create_batch(db, payload)
    except ImportServiceError as exc:
        _raise_import_http_error(exc)
    await _audit(
        db,
        request,
        action="import.batch.create",
        status_="success",
        target=batch.id,
        detail={"created": created, "mode": batch.mode, "status": batch.status},
    )
    return ImportBatchResponse.model_validate(import_batch_to_dict(batch))


@router.post(
    "/import/batches/{batch_id}/messages",
    response_model=ImportBatchMessagesResponse,
    summary="UPSERT a Collector message chunk",
)
async def import_batch_messages(
    batch_id: str,
    payload: ImportBatchMessagesRequest,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
    _: None = Depends(require_admin_role("operator", "admin")),
) -> ImportBatchMessagesResponse:
    try:
        result = await ImportService.import_batch_messages(db, batch_id=batch_id, payload=payload)
    except ImportServiceError as exc:
        _raise_import_http_error(exc)
    await _audit(
        db,
        request,
        action="import.batch.messages",
        status_="partial" if result.failed else "success",
        target=batch_id,
        detail={
            "inserted": result.inserted,
            "updated": result.updated,
            "unchanged": result.unchanged,
            "failed": result.failed,
            "replayed": result.replayed,
        },
    )
    return result


@router.post(
    "/import/batches/{batch_id}/complete",
    response_model=ImportBatchResponse,
    summary="Complete a Collector import batch",
)
async def complete_import_batch(
    batch_id: str,
    payload: ImportBatchFinishRequest,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
    _: None = Depends(require_admin_role("operator", "admin")),
) -> ImportBatchResponse:
    try:
        batch = await ImportService.finish_batch(
            db,
            batch_id=batch_id,
            failed=False,
            partial=payload.partial,
            detail=payload.detail,
        )
    except ImportServiceError as exc:
        _raise_import_http_error(exc)
    await _audit(
        db,
        request,
        action="import.batch.complete",
        status_="success",
        target=batch_id,
        detail={"status": batch.status},
    )
    return ImportBatchResponse.model_validate(import_batch_to_dict(batch))


@router.post(
    "/import/batches/{batch_id}/fail",
    response_model=ImportBatchResponse,
    summary="Fail a Collector import batch",
)
async def fail_import_batch(
    batch_id: str,
    payload: ImportBatchFinishRequest,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
    _: None = Depends(require_admin_role("operator", "admin")),
) -> ImportBatchResponse:
    try:
        batch = await ImportService.finish_batch(
            db,
            batch_id=batch_id,
            failed=True,
            partial=payload.partial,
            detail=payload.detail,
            error_code=payload.error_code,
            error_detail=payload.error_detail,
        )
    except ImportServiceError as exc:
        _raise_import_http_error(exc)
    await _audit(
        db,
        request,
        action="import.batch.fail",
        status_="success",
        target=batch_id,
        detail={"status": batch.status, "error_code": payload.error_code},
    )
    return ImportBatchResponse.model_validate(import_batch_to_dict(batch))


@router.post("/external/media", response_model=ExternalMediaUploadResponse, status_code=status.HTTP_201_CREATED)
async def upload_external_media(
    request: Request,
    db: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
    _: None = Depends(require_admin_role("operator", "admin")),
) -> ExternalMediaUploadResponse:
    fields, files = _parse_external_multipart(request.headers.get("content-type", ""), await request.body())
    uploaded = files.get("file")
    if uploaded is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="file field is required")

    content = uploaded["content"]
    if not content:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="file content is empty")
    if len(content) > settings.media_max_bytes:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="file exceeds MEDIA_MAX_BYTES")

    media_type = _normalize_external_media_type(fields.get("media_type"))
    if media_type not in _EXTERNAL_MEDIA_TYPES:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="unsupported media_type")

    file_name = (fields.get("file_name") or uploaded.get("file_name") or "").strip() or None
    file_hash = hashlib.md5(content).hexdigest()
    local_path = await MessageService.save_media_asset(
        db,
        file_content=content,
        file_type=media_type,
        ext=_external_media_ext(file_name, media_type),
        storage_root=settings.storage_root,
        public_prefix=settings.public_storage_prefix,
    )
    return ExternalMediaUploadResponse(
        local_path=local_path,
        media_type=media_type,
        file_name=file_name,
        file_size=len(content),
        file_hash=file_hash,
    )


@router.get("/search", response_model=list[MessageResponse])
async def search_messages(
    robot_id: str = Query(..., min_length=1),
    keyword: str | None = Query(default=None),
    room_id: str | None = Query(default=None),
    message_type: str | None = Query(default=None, pattern="^(group|private)$"),
    sender_id: str | None = Query(default=None),
    start_timestamp: int | None = Query(default=None),
    end_timestamp: int | None = Query(default=None),
    media_state: str | None = Query(default=None, pattern="^(all|any|complete|not_downloaded|missing|failed)$"),
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db_session),
) -> list[MessageResponse]:
    try:
        messages = await QueryService.search_messages(
            db,
            robot_id=robot_id,
            keyword=keyword,
            room_id=room_id,
            message_type=message_type,
            sender_id=sender_id,
            start_timestamp=start_timestamp,
            end_timestamp=end_timestamp,
            media_state=media_state,
            limit=limit,
        )
    except AmbiguousConversationError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
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
                forward_depth=settings.forward_cache_max_depth,
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
        reason_summary=report.reason_summary,
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
        not_downloaded_media=report.not_downloaded_media,
        not_downloaded_videos=report.not_downloaded_videos,
        thumbnail_only_videos=report.thumbnail_only_videos,
        source_missing_media=report.source_missing_media,
        media_parse_failures=report.media_parse_failures,
        media_hash_mismatches=report.media_hash_mismatches,
        reason_summary=report.reason_summary,
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
            "unrepaired_media_files": report.unrepaired_media_files,
            "media_hash_mismatches": report.media_hash_mismatches,
            "repaired_profile_avatars": report.repaired_profile_avatars,
        },
    )
    return OfflineRepairResponse(
        scanned_messages=report.scanned_messages,
        repaired_media_assets=report.repaired_media_assets,
        repaired_media_files=report.repaired_media_files,
        repaired_file_sizes=report.repaired_file_sizes,
        repaired_profile_avatars=report.repaired_profile_avatars,
        unrepaired_media_files=report.unrepaired_media_files,
        media_hash_mismatches=report.media_hash_mismatches,
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
        async def load_forward(nested_forward_id: str) -> dict:
            return await OneBotRPCService.call_action(robot_id, "get_forward_msg", {"id": nested_forward_id})

        localized = await MediaService.localize_onebot_payload(
            db,
            payload,
            http_client=client,
            storage_root=settings.storage_root,
            public_prefix=settings.public_storage_prefix,
            max_bytes=settings.media_max_bytes,
            forward_depth=settings.forward_cache_max_depth,
        )
        localized = await MediaService.cache_nested_forward_payloads(
            db,
            localized,
            forward_loader=load_forward,
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


@router.get(
    "/export",
    summary="Export chat backup package",
    description="Bounded v3 compatibility export for filtered/small datasets. Full backups must use /api/backup/run and the isolated v4 worker. Set compressed=true to download a .json.gz package.",
    responses={
        200: {
            "description": "Backup package as JSON or gzip",
            "content": {
                "application/json": {"example": {"manifest": {"schema": "chat-audit-core.backup.v3"}, "messages": []}},
                "application/gzip": {"schema": {"type": "string", "format": "binary"}},
            },
        }
    },
)
async def export_data(
    robot_id: str | None = Query(default=None, min_length=1),
    room_id: str | None = Query(default=None, min_length=1),
    message_type: str | None = Query(default=None, pattern="^(group|private)$"),
    start_timestamp: int | None = Query(default=None),
    end_timestamp: int | None = Query(default=None),
    compressed: bool = Query(default=False),
    db: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
):
    if compressed:
        try:
            payload = await BackupService.export_package_compressed(
                db,
                robot_id=robot_id,
                room_id=room_id,
                message_type=message_type,
                start_timestamp=start_timestamp,
                end_timestamp=end_timestamp,
                storage_root=settings.storage_root,
                public_storage_prefix=settings.public_storage_prefix,
                max_media_bytes=settings.media_max_bytes,
                system_id=settings.system_instance_id,
                signing_key=settings.app_secret_key,
                max_messages=settings.backup_legacy_export_max_messages,
                max_total_media_bytes=settings.backup_legacy_export_max_total_media_bytes,
            )
        except AmbiguousConversationError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=str(exc)) from exc
        filename = f"chat-audit-export-{int(time.time())}.json.gz"
        return Response(
            content=payload,
            media_type="application/gzip",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    try:
        return await BackupService.export_package(
            db,
            robot_id=robot_id,
            room_id=room_id,
            message_type=message_type,
            start_timestamp=start_timestamp,
            end_timestamp=end_timestamp,
            storage_root=settings.storage_root,
            public_storage_prefix=settings.public_storage_prefix,
            max_media_bytes=settings.media_max_bytes,
            system_id=settings.system_instance_id,
            signing_key=settings.app_secret_key,
            max_messages=settings.backup_legacy_export_max_messages,
            max_total_media_bytes=settings.backup_legacy_export_max_total_media_bytes,
        )
    except AmbiguousConversationError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=str(exc)) from exc


async def _read_import_package_request(request: Request, *, max_bytes: int) -> dict[str, Any]:
    if max_bytes <= 0:
        raise HTTPException(status_code=503, detail="legacy import is disabled by its size limit")
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > max_bytes:
                raise HTTPException(status_code=413, detail=f"legacy import exceeds {max_bytes} bytes")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid Content-Length header") from exc
    payload = bytearray()
    async for chunk in request.stream():
        if len(payload) + len(chunk) > max_bytes:
            raise HTTPException(status_code=413, detail=f"legacy import exceeds {max_bytes} bytes")
        payload.extend(chunk)
    try:
        return BackupService.decode_package_bytes(bytes(payload), max_decoded_bytes=max_bytes)
    except ValueError as exc:
        if str(exc).startswith("legacy import"):
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        raise HTTPException(status_code=400, detail=f"invalid import package: {exc}") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail=f"invalid import package: {exc}") from exc


@router.post(
    "/import/validate",
    response_model=ImportValidationResponse,
    summary="Validate an import package",
    description="Preview a bounded legacy v1-v3 JSON/gzip package before importing. Use the backup CLI to validate or restore v4 .cacb archives and large legacy packages.",
)
async def validate_import_data(
    request: Request,
    db: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> ImportValidationResponse:
    package = await _read_import_package_request(request, max_bytes=settings.backup_legacy_import_max_bytes)
    report = await BackupService.preview_import_package(
        db,
        package,
        storage_root=settings.storage_root,
        public_storage_prefix=settings.public_storage_prefix,
        signing_key=settings.app_secret_key,
        require_signature=settings.backup_import_require_signature,
    )
    return ImportValidationResponse(**report)


@router.post(
    "/import",
    response_model=ImportResultResponse,
    response_model_exclude_defaults=True,
    summary="Import a backup package",
    description="Import a bounded legacy v1-v3 JSON/gzip package. Requires admin role because existing records may be updated. Full v4 restore uses the isolated backup CLI and an empty target.",
    responses={400: {"description": "Package validation failed"}, 403: {"description": "Admin role required"}},
)
async def import_data(
    request: Request,
    db: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
    _: None = Depends(require_admin_role("admin")),
) -> ImportResultResponse:
    action = "import.run"
    _enforce_high_risk_rate_limit(request, action, settings)
    package = await _read_import_package_request(request, max_bytes=settings.backup_legacy_import_max_bytes)
    manifest = package.get("manifest") if isinstance(package.get("manifest"), dict) else {}
    package_schema = manifest.get("schema")
    try:
        result = await BackupService.import_package(
            db,
            package,
            storage_root=settings.storage_root,
            public_storage_prefix=settings.public_storage_prefix,
            signing_key=settings.app_secret_key,
            require_signature=settings.backup_import_require_signature,
        )
    except ValueError as exc:
        await db.rollback()
        BackupService.write_failure_log(settings.backup_root, event="import", error=str(exc), context={"schema": package_schema})
        await _audit(db, request, action=action, status_="failed", detail={"error": str(exc), "schema": package_schema})
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _audit(db, request, action=action, status_="success", detail={"messages": result["messages"], "robot_messages": result["robot_messages"], "media_assets": result["media_assets"]})
    return ImportResultResponse(**result)
