import asyncio
import contextlib
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.api import public_router as public_api_router, router as api_router
from app.config import Settings, get_settings
from app.database import AsyncSessionLocal, backfill_bot_profiles, create_all_tables, engine as default_engine, ensure_schema_compatibility, get_db_session
from app.schemas import HealthResponse
from app.services.backup_service import start_auto_backup_scheduler
from app.ws import router as ws_router


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


def create_app(
    settings: Settings | None = None,
    engine: AsyncEngine | None = None,
    sessionmaker: async_sessionmaker[AsyncSession] | None = None,
) -> FastAPI:
    active_settings = settings or get_settings()
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

    async def app_db_session() -> AsyncIterator[AsyncSession]:
        async with active_sessionmaker() as session:
            yield session

    app.dependency_overrides[get_db_session] = app_db_session
    app.mount(
        active_settings.public_storage_prefix,
        StaticFiles(directory=active_settings.storage_root, check_dir=False),
        name="storage",
    )
    app.include_router(public_api_router, prefix="/api")
    app.include_router(api_router, prefix="/api")
    app.include_router(ws_router)

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(index_file, media_type="text/html")

    @app.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse(status="ok", app=active_settings.app_name)

    return app


app = create_app()
