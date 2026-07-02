from fastapi import FastAPI

from app.api import router as api_router
from app.config import get_settings
from app.schemas import HealthResponse
from app.ws import router as ws_router

settings = get_settings()
app = FastAPI(title=settings.app_name)
app.include_router(api_router, prefix="/api")
app.include_router(ws_router)


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok", app=settings.app_name)
