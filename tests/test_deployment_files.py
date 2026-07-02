from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_dockerfile_installs_runtime_dependencies_and_runs_uvicorn():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "python:3.11-slim" in dockerfile
    assert "ffmpeg" in dockerfile
    assert "pip install" in dockerfile
    assert "requirements.txt" in dockerfile
    assert "uvicorn" in dockerfile
    assert "app.main:app" in dockerfile


def test_docker_compose_defines_app_postgres_volumes_and_healthcheck():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))

    services = compose["services"]
    assert set(services) == {"app", "postgres"}
    assert services["app"]["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert "8000:8000" in services["app"]["ports"]
    assert "./data/storage:/app/data/storage" in services["app"]["volumes"]
    assert "./data/backups:/app/data/backups" in services["app"]["volumes"]
    assert services["app"]["environment"]["DATABASE_URL"].startswith("postgresql+asyncpg://")
    assert "healthcheck" in services["postgres"]
    assert "postgres_data" in compose["volumes"]


def test_dockerignore_excludes_runtime_and_secret_files():
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")

    assert ".venv" in dockerignore
    assert ".env" in dockerignore
    assert "data/storage/*" in dockerignore
    assert "data/backups/*" in dockerignore
    assert ".git" in dockerignore
