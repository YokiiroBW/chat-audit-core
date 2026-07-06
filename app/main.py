import asyncio
import contextlib
import json
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.api import public_router as public_api_router, router as api_router
from app.config import Settings, get_settings
from app.database import AsyncSessionLocal, backfill_bot_profiles, create_all_tables, engine as default_engine, ensure_schema_compatibility, get_db_session
from app.logging_config import setup_logging
from app.metrics import metrics_registry
from app.schemas import HealthResponse
from app.services.backup_service import start_auto_backup_scheduler
from app.services.runtime_service import RuntimeService
from app.ws import router as ws_router

CSRF_COOKIE_NAME = "chat_audit_csrf"
CSRF_HEADER_NAME = "x-csrf-token"
CSRF_SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
CSRF_EXEMPT_PATHS = {"/api/auth/login"}
logger = logging.getLogger(__name__)


def _is_browser_context(request: Request) -> bool:
    return any(
        request.headers.get(header)
        for header in ("sec-fetch-site", "origin", "referer")
    )


def _is_cross_site_request(request: Request) -> bool:
    return request.headers.get("sec-fetch-site", "").lower() == "cross-site"


def _new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def _set_csrf_cookie(response, settings: Settings, token: str) -> None:
    response.set_cookie(
        CSRF_COOKIE_NAME,
        token,
        httponly=False,
        secure=settings.csrf_secure_cookie,
        samesite="strict",
        max_age=86400 * 7,
    )


def _csrf_response(settings: Settings) -> JSONResponse:
    response = JSONResponse({"detail": "CSRF token missing or invalid"}, status_code=403)
    _set_csrf_cookie(response, settings, _new_csrf_token())
    return response


def _check_writable_directory(path: Path) -> str:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".health_check_{secrets.token_hex(8)}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return "ok"
    except Exception as exc:
        logger.warning("Health check directory probe failed", extra={"path": str(path), "error": str(exc)})
        return "error"


def _has_configured_admin_tokens(settings: Settings) -> bool:
    if settings.admin_api_token.strip():
        return True
    raw_tokens = settings.admin_api_tokens.strip()
    if not raw_tokens:
        return False
    try:
        parsed = json.loads(raw_tokens)
    except json.JSONDecodeError as exc:
        raise ValueError("ADMIN_API_TOKENS must be valid JSON in production") from exc
    if isinstance(parsed, list):
        return any(isinstance(item, dict) and str(item.get("token") or "").strip() for item in parsed)
    if isinstance(parsed, dict):
        return any(str(token).strip() for token in parsed.keys())
    return False


def validate_production_settings(settings: Settings) -> None:
    if settings.app_env.lower() != "production":
        return
    unsafe_secret_values = {"", "change-me", "change-me-in-production", "replace-with-a-long-random-secret"}
    if settings.app_secret_key in unsafe_secret_values:
        raise ValueError("APP_SECRET_KEY must be set to a non-default value in production")
    unsafe_onebot_values = {"", "replace-with-onebot-access-token"}
    if settings.onebot_access_token.strip() in unsafe_onebot_values:
        raise ValueError("ONEBOT_ACCESS_TOKEN must be set to a non-default value in production")
    unsafe_admin_values = {"", "replace-with-admin-api-token"}
    if settings.admin_api_token.strip() in unsafe_admin_values and not _has_configured_admin_tokens(settings):
        raise ValueError("ADMIN_API_TOKEN must be set to a non-default value in production")
    if "sqlite" in settings.database_url.lower():
        raise ValueError("DATABASE_URL must use PostgreSQL or another production database in production")
    unsafe_instance_ids = {"", "chat-audit-core"}
    if settings.system_instance_id.strip() in unsafe_instance_ids:
        raise ValueError("SYSTEM_INSTANCE_ID must be unique in production")
    for name, path in (("STORAGE_ROOT", settings.storage_root), ("BACKUP_ROOT", settings.backup_root)):
        try:
            path.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            raise ValueError(f"{name} {path} cannot be created: {exc}") from exc
        if not os.access(path, os.W_OK):
            raise ValueError(f"{name} {path} is not writable")


def create_app(
    settings: Settings | None = None,
    engine: AsyncEngine | None = None,
    sessionmaker: async_sessionmaker[AsyncSession] | None = None,
) -> FastAPI:
    active_settings = settings or get_settings()
    setup_logging(active_settings.log_level)
    validate_production_settings(active_settings)
    active_engine = engine or default_engine
    active_sessionmaker = sessionmaker or AsyncSessionLocal
    static_dir = Path(__file__).resolve().parent / "static"
    index_file = static_dir / "index.html"

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        active_settings.storage_root.mkdir(parents=True, exist_ok=True)
        active_settings.backup_root.mkdir(parents=True, exist_ok=True)
        await create_all_tables(active_engine)
        await ensure_schema_compatibility(active_engine)
        await backfill_bot_profiles(active_sessionmaker)
        backup_task = start_auto_backup_scheduler(settings=active_settings, sessionmaker=active_sessionmaker)
        try:
            yield
        finally:
            if backup_task is not None:
                backup_task.cancel()
                if hasattr(backup_task, "__await__"):
                    with contextlib.suppress(asyncio.CancelledError):
                        await backup_task

    app = FastAPI(title=active_settings.app_name, lifespan=lifespan)

    @app.middleware("http")
    async def request_logging_middleware(request: Request, call_next):
        started_at = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
            endpoint = request.scope.get("route").path if request.scope.get("route") else request.url.path
            metrics_registry.record_http_request(
                method=request.method,
                endpoint=endpoint,
                status_code=500,
                duration_seconds=duration_ms / 1000,
            )
            logger.exception(
                "HTTP request failed",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "duration_ms": duration_ms,
                    "client": request.client.host if request.client else None,
                },
            )
            raise
        duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
        endpoint = request.scope.get("route").path if request.scope.get("route") else request.url.path
        metrics_registry.record_http_request(
            method=request.method,
            endpoint=endpoint,
            status_code=response.status_code,
            duration_seconds=duration_ms / 1000,
        )
        logger.info(
            "HTTP request completed",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": duration_ms,
                "client": request.client.host if request.client else None,
            },
        )
        return response

    @app.middleware("http")
    async def request_size_limit_middleware(request: Request, call_next):
        content_length = request.headers.get("content-length")
        if content_length and content_length.isdigit() and int(content_length) > active_settings.api_max_request_body_bytes:
            return JSONResponse({"detail": "Request body too large"}, status_code=413)
        return await call_next(request)

    @app.middleware("http")
    async def csrf_middleware(request: Request, call_next):
        if not active_settings.csrf_enabled:
            return await call_next(request)

        csrf_cookie = request.cookies.get(CSRF_COOKIE_NAME)
        needs_cookie = csrf_cookie is None
        if request.method not in CSRF_SAFE_METHODS and request.url.path not in CSRF_EXEMPT_PATHS and _is_browser_context(request):
            if _is_cross_site_request(request):
                return _csrf_response(active_settings)
            csrf_header = request.headers.get(CSRF_HEADER_NAME)
            if not csrf_cookie or not csrf_header or not secrets.compare_digest(csrf_cookie, csrf_header):
                return _csrf_response(active_settings)

        response = await call_next(request)
        if needs_cookie:
            _set_csrf_cookie(response, active_settings, _new_csrf_token())
        return response

    async def app_db_session() -> AsyncIterator[AsyncSession]:
        async with active_sessionmaker() as session:
            yield session

    app.dependency_overrides[get_db_session] = app_db_session
    app.mount(
        active_settings.public_storage_prefix,
        StaticFiles(directory=active_settings.storage_root, check_dir=False),
        name="storage",
    )
    app.mount(
        "/assets",
        StaticFiles(directory=static_dir / "assets", check_dir=False),
        name="assets",
    )
    app.include_router(public_api_router, prefix="/api")
    app.include_router(api_router, prefix="/api")
    app.include_router(ws_router)

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(index_file, media_type="text/html")

    @app.get("/health", response_model=HealthResponse)
    async def health() -> JSONResponse | HealthResponse:
        checks: dict[str, str] = {
            "app": "ok",
            "database": "unknown",
            "storage": "unknown",
            "backup": "unknown",
        }
        try:
            async with active_sessionmaker() as session:
                await session.execute(sql_text("SELECT 1"))
            checks["database"] = "ok"
        except Exception as exc:
            logger.warning("Health check database probe failed", extra={"error": str(exc)})
            checks["database"] = "error"

        checks["storage"] = _check_writable_directory(active_settings.storage_root)
        checks["backup"] = _check_writable_directory(active_settings.backup_root)

        if active_settings.media_transcode_enabled:
            ffmpeg = RuntimeService.ffmpeg_status(active_settings)
            checks["ffmpeg"] = "ok" if ffmpeg["ffmpeg_available"] else "error"

        status_value = "ok" if all(value == "ok" for value in checks.values()) else "degraded"
        payload = HealthResponse(status=status_value, app=active_settings.app_name, checks=checks).model_dump()
        if status_value != "ok":
            return JSONResponse(payload, status_code=503)
        return HealthResponse(**payload)

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        return Response(content=metrics_registry.render_prometheus(), media_type="text/plain; version=0.0.4")

    return app


app = create_app()
