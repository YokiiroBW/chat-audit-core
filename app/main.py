from fastapi import FastAPI

from app.config import get_settings
from app.schemas import HealthResponse

settings = get_settings()
app = FastAPI(title=settings.app_name)


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok", app=settings.app_name)
