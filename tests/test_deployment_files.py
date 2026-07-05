from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_dockerfile_uses_offline_friendly_runtime_and_runs_uvicorn():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "python:3.11-slim" in dockerfile
    assert "apt-get install -y --no-install-recommends ffmpeg" in dockerfile
    assert "rm -rf /var/lib/apt/lists/*" in dockerfile
    assert "curl" not in dockerfile
    assert "pip install" in dockerfile
    assert "requirements.txt" in dockerfile
    assert "uvicorn" in dockerfile
    assert "app.main:app" in dockerfile
    assert "urllib.request" in dockerfile


def test_docker_compose_defines_app_postgres_volumes_and_healthcheck():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))

    services = compose["services"]
    assert set(services) == {"app", "postgres"}
    assert services["app"]["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert "8000:8000" in services["app"]["ports"]
    assert "./data/storage:/app/data/storage" in services["app"]["volumes"]
    assert "./data/backups:/app/data/backups" in services["app"]["volumes"]
    assert services["app"]["environment"]["DATABASE_URL"] == "postgresql+asyncpg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@postgres:5432/${POSTGRES_DB}"
    assert services["postgres"]["environment"]["POSTGRES_PASSWORD"] == "${POSTGRES_PASSWORD}"
    assert services["app"]["environment"]["APP_SECRET_KEY"] == "${APP_SECRET_KEY}"
    assert services["app"]["environment"]["ADMIN_API_TOKEN"] == "${ADMIN_API_TOKEN}"
    assert services["app"]["environment"]["ONEBOT_ACCESS_TOKEN"] == "${ONEBOT_ACCESS_TOKEN}"
    assert services["app"]["environment"]["FFMPEG_BIN"] == "ffmpeg"
    assert services["app"]["environment"]["MEDIA_TRANSCODE_ENABLED"] == "${MEDIA_TRANSCODE_ENABLED:-false}"
    assert services["app"]["environment"]["MEDIA_TRANSCODE_VOICE_EXT"] == "mp3"
    assert services["app"]["environment"]["MEDIA_TRANSCODE_VIDEO_EXT"] == "mp4"
    assert services["app"]["environment"]["AUTO_BACKUP_CRON"] == "0 3 * * *"
    assert services["app"]["environment"]["AUTO_BACKUP_KEEP_LATEST"] == 7
    assert "healthcheck" in services["postgres"]
    assert "postgres_data" in compose["volumes"]

    app_healthcheck = " ".join(services["app"]["healthcheck"]["test"])
    assert "python" in app_healthcheck
    assert "urllib.request" in app_healthcheck
    assert "curl" not in app_healthcheck


def test_dockerignore_excludes_runtime_and_secret_files():
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")

    assert ".venv" in dockerignore
    assert ".env" in dockerignore
    assert "data/storage/*" in dockerignore
    assert "data/backups/*" in dockerignore
    assert ".git" in dockerignore
