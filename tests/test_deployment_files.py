from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_dockerfile_uses_offline_friendly_runtime_and_runs_uvicorn():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "python:3.11-slim" in dockerfile
    assert "apt-get" not in dockerfile
    assert "curl" not in dockerfile
    assert "pip install" in dockerfile
    assert "requirements.txt" in dockerfile
    assert "alembic.ini" in dockerfile
    assert "migrations" in dockerfile
    assert "uvicorn" in dockerfile
    assert "app.main:app" in dockerfile
    assert "urllib.request" in dockerfile


def test_optional_ffmpeg_dockerfile_installs_ffmpeg_explicitly():
    dockerfile = (ROOT / "Dockerfile.ffmpeg").read_text(encoding="utf-8")

    assert "python:3.11-slim" in dockerfile
    assert "apt-get" not in dockerfile
    assert "COPY vendor/wheels ./vendor/wheels" in dockerfile
    assert "pip install --no-index --find-links ./vendor/wheels imageio-ffmpeg==0.6.0" in dockerfile
    assert "imageio-ffmpeg==0.6.0" in dockerfile
    assert "imageio_ffmpeg.get_ffmpeg_exe()" in dockerfile
    assert "/usr/local/bin/ffmpeg" in dockerfile
    assert "ffmpeg -version" in dockerfile
    assert (ROOT / "vendor/wheels/imageio_ffmpeg-0.6.0-py3-none-manylinux2014_x86_64.whl").exists()
    assert "alembic.ini" in dockerfile
    assert "migrations" in dockerfile
    assert "uvicorn" in dockerfile


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
    assert services["app"]["environment"]["ADMIN_API_TOKENS"] == "${ADMIN_API_TOKENS:-}"
    assert services["app"]["environment"]["ONEBOT_ACCESS_TOKEN"] == "${ONEBOT_ACCESS_TOKEN}"
    assert services["app"]["environment"]["FFMPEG_BIN"] == "ffmpeg"
    assert services["app"]["environment"]["FFMPEG_LIBRARY_PATH"] == "${FFMPEG_LIBRARY_PATH:-}"
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


def test_optional_ffmpeg_compose_override_uses_ffmpeg_dockerfile():
    compose = yaml.safe_load((ROOT / "docker-compose.ffmpeg.yml").read_text(encoding="utf-8"))

    app = compose["services"]["app"]
    assert app["build"]["dockerfile"] == "Dockerfile.ffmpeg"
    assert app["environment"]["MEDIA_TRANSCODE_ENABLED"] == "true"
    assert app["environment"]["FFMPEG_BIN"] == "ffmpeg"


def test_host_ffmpeg_compose_override_mounts_existing_binary():
    compose = yaml.safe_load((ROOT / "docker-compose.ffmpeg-host.yml").read_text(encoding="utf-8"))

    app = compose["services"]["app"]
    assert app["environment"]["MEDIA_TRANSCODE_ENABLED"] == "true"
    assert app["environment"]["FFMPEG_BIN"] == "/opt/host-bin/ffmpeg"
    assert app["environment"]["FFMPEG_LIBRARY_PATH"] == "/opt/host-lib64:/opt/host-usr-lib"
    assert "${FFMPEG_HOST_BIN:?Set FFMPEG_HOST_BIN to the host ffmpeg executable path}:/opt/host-bin/ffmpeg:ro" in app["volumes"]
    assert "${FFMPEG_HOST_LIB64:-/lib64}:/opt/host-lib64:ro" in app["volumes"]
    assert "${FFMPEG_HOST_USR_LIB:-/usr/lib}:/opt/host-usr-lib:ro" in app["volumes"]


def test_dockerignore_excludes_runtime_and_secret_files():
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")

    assert ".venv" in dockerignore
    assert ".env" in dockerignore
    assert "data/storage/*" in dockerignore
    assert "data/backups/*" in dockerignore
    assert ".git" in dockerignore
