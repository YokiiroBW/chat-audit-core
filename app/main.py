import asyncio
import contextlib
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.api import router as api_router
from app.config import Settings, get_settings
from app.database import AsyncSessionLocal, create_all_tables, engine as default_engine, get_db_session
from app.schemas import HealthResponse
from app.services.backup_service import start_auto_backup_scheduler
from app.ws import router as ws_router


def create_app(
    settings: Settings | None = None,
    engine: AsyncEngine | None = None,
    sessionmaker: async_sessionmaker[AsyncSession] | None = None,
) -> FastAPI:
    active_settings = settings or get_settings()
    active_engine = engine or default_engine
    active_sessionmaker = sessionmaker or AsyncSessionLocal
    static_dir = Path(__file__).resolve().parent / "static"
    index_file = static_dir / "index.html"

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        active_settings.storage_root.mkdir(parents=True, exist_ok=True)
        active_settings.backup_root.mkdir(parents=True, exist_ok=True)
        await create_all_tables(active_engine)
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
