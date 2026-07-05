from httpx import ASGITransport, AsyncClient
import pytest

from app.config import Settings, get_settings
from app.database import get_db_session
from app.main import app
from app.services.runtime_service import RuntimeService


def test_runtime_service_reports_missing_ffmpeg(monkeypatch):
    monkeypatch.setattr("app.services.runtime_service.shutil.which", lambda _: None)

    status = RuntimeService.ffmpeg_status(Settings(ffmpeg_bin="missing-ffmpeg"))

    assert status["ffmpeg_bin"] == "missing-ffmpeg"
    assert status["ffmpeg_available"] is False
    assert status["ffmpeg_path"] is None


def test_runtime_service_reports_ffmpeg_version(monkeypatch):
    seen = {}

    class Completed:
        returncode = 0
        stdout = "ffmpeg version 6.1-test\nbuilt with test"
        stderr = ""

    monkeypatch.setattr("app.services.runtime_service.shutil.which", lambda _: "C:/tools/ffmpeg.exe")

    def fake_run(*args, **kwargs):
        seen["args"] = args
        seen["env"] = kwargs.get("env")
        return Completed()

    monkeypatch.setattr("app.services.runtime_service.subprocess.run", fake_run)

    status = RuntimeService.ffmpeg_status(
        Settings(
            ffmpeg_bin="ffmpeg",
            ffmpeg_library_path="/opt/host-lib64:/opt/host-usr-lib",
            media_transcode_enabled=True,
        )
    )

    assert status["media_transcode_enabled"] is True
    assert status["ffmpeg_library_path"] == "/opt/host-lib64:/opt/host-usr-lib"
    assert status["ffmpeg_available"] is True
    assert status["ffmpeg_path"] == "C:/tools/ffmpeg.exe"
    assert status["ffmpeg_version"] == "ffmpeg version 6.1-test"
    assert seen["env"]["LD_LIBRARY_PATH"] == "/opt/host-lib64:/opt/host-usr-lib"


@pytest.mark.asyncio
async def test_runtime_status_api(db_session, monkeypatch):
    async def override_db_session():
        yield db_session

    monkeypatch.setattr("app.services.runtime_service.shutil.which", lambda _: None)
    app.dependency_overrides[get_db_session] = override_db_session
    app.dependency_overrides[get_settings] = lambda: Settings(ffmpeg_bin="missing-ffmpeg")
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/system/runtime")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["ffmpeg_bin"] == "missing-ffmpeg"
    assert response.json()["ffmpeg_library_path"] == ""
    assert response.json()["ffmpeg_available"] is False
