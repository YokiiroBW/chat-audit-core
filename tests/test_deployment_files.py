from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_dockerfile_uses_offline_friendly_runtime_and_runs_uvicorn():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "python:3.11-slim" in dockerfile
    assert "FROM python:3.11-slim AS builder" in dockerfile
    assert "COPY --from=builder /root/.local /root/.local" in dockerfile
    assert "apt-get" not in dockerfile
    assert "curl" not in dockerfile
    assert "pip install" in dockerfile
    assert "requirements-prod.txt" in dockerfile
    assert "alembic.ini" in dockerfile
    assert "migrations" in dockerfile
    assert "uvicorn" in dockerfile
    assert "app.main:app" in dockerfile
    assert "urllib.request" in dockerfile


def test_optional_ffmpeg_dockerfile_installs_ffmpeg_explicitly():
    dockerfile = (ROOT / "Dockerfile.ffmpeg").read_text(encoding="utf-8")

    assert "python:3.11-slim" in dockerfile
    assert "FROM python:3.11-slim AS builder" in dockerfile
    assert "COPY --from=builder /root/.local /root/.local" in dockerfile
    assert "apt-get" not in dockerfile
    assert "COPY vendor/wheels ./vendor/wheels" in dockerfile
    assert "pip install --user --no-index --find-links ./vendor/wheels imageio-ffmpeg==0.6.0" in dockerfile
    assert "requirements-prod.txt" in dockerfile
    assert "imageio-ffmpeg==0.6.0" in dockerfile
    assert "imageio_ffmpeg.get_ffmpeg_exe()" in dockerfile
    assert "/usr/local/bin/ffmpeg" in dockerfile
    assert "ffmpeg -version" in dockerfile
    assert (ROOT / "vendor/wheels/imageio_ffmpeg-0.6.0-py3-none-manylinux2014_x86_64.whl").exists()
    assert "alembic.ini" in dockerfile
    assert "migrations" in dockerfile
    assert "uvicorn" in dockerfile


def test_requirements_include_structured_logging_dependency():
    requirements = (ROOT / "requirements-prod.txt").read_text(encoding="utf-8")
    dev_requirements = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
    default_requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")

    assert "python-json-logger==2.0.7" in requirements
    assert "pytest==" not in requirements
    assert "-r requirements-prod.txt" in dev_requirements
    assert "pytest==" in dev_requirements
    assert default_requirements.strip() == "-r requirements-dev.txt"


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
    assert services["app"]["environment"]["SYSTEM_INSTANCE_ID"] == "${SYSTEM_INSTANCE_ID}"
    assert services["app"]["environment"]["ADMIN_API_TOKEN"] == "${ADMIN_API_TOKEN}"
    assert services["app"]["environment"]["ADMIN_API_TOKENS"] == "${ADMIN_API_TOKENS:-}"
    assert services["app"]["environment"]["ONEBOT_ACCESS_TOKEN"] == "${ONEBOT_ACCESS_TOKEN}"
    assert services["app"]["environment"]["LOG_LEVEL"] == "${LOG_LEVEL:-INFO}"
    assert services["app"]["environment"]["FFMPEG_BIN"] == "ffmpeg"
    assert services["app"]["environment"]["FFMPEG_LIBRARY_PATH"] == "${FFMPEG_LIBRARY_PATH:-}"
    assert services["app"]["environment"]["MEDIA_TRANSCODE_ENABLED"] == "${MEDIA_TRANSCODE_ENABLED:-false}"
    assert services["app"]["environment"]["MEDIA_TRANSCODE_VOICE_EXT"] == "mp3"
    assert services["app"]["environment"]["MEDIA_TRANSCODE_VIDEO_EXT"] == "mp4"
    assert services["app"]["environment"]["AUTO_BACKUP_CRON"] == "0 3 * * *"
    assert services["app"]["environment"]["AUTO_BACKUP_KEEP_LATEST"] == 7
    assert "healthcheck" in services["postgres"]
    assert "postgres_data" in compose["volumes"]

    postgres_healthcheck = " ".join(services["postgres"]["healthcheck"]["test"])
    assert "pg_isready" in postgres_healthcheck
    assert "psql" in postgres_healthcheck
    assert "SELECT 1" in postgres_healthcheck
    assert services["postgres"]["healthcheck"]["start_period"] == "10s"

    app_healthcheck = " ".join(services["app"]["healthcheck"]["test"])
    assert "python" in app_healthcheck
    assert "urllib.request" in app_healthcheck
    assert "sys.exit" in app_healthcheck
    assert "status" in app_healthcheck
    assert "curl" not in app_healthcheck
    assert services["app"]["healthcheck"]["timeout"] == "10s"
    assert services["app"]["healthcheck"]["retries"] == 5


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


def test_static_assets_include_minified_frontend_bundle():
    source_js = ROOT / "app/static/assets/app.js"
    source_css = ROOT / "app/static/assets/app.css"
    min_js = ROOT / "app/static/assets/app.min.js"
    min_css = ROOT / "app/static/assets/app.min.css"
    minifier = ROOT / "scripts/minify_static_assets.py"

    assert minifier.exists()
    assert min_js.exists()
    assert min_css.exists()
    assert min_js.stat().st_size < source_js.stat().st_size
    assert min_css.stat().st_size < source_css.stat().st_size


def test_disaster_recovery_document_covers_restore_drill_and_targets():
    guide = (ROOT / "DISASTER_RECOVERY.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "RTO" in guide
    assert "RPO" in guide
    assert "AUTO_BACKUP_CRON" in guide
    assert "AUTO_BACKUP_KEEP_LATEST" in guide
    assert "/api/import/validate" in guide
    assert "/api/offline/audit" in guide
    assert "/api/offline/repair" in guide
    assert "演练流程" in guide
    assert "DISASTER_RECOVERY.md" in readme


def test_contributing_and_architecture_documents_cover_onboarding():
    contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    architecture = (ROOT / "ARCHITECTURE.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "开发环境" in contributing
    assert "pytest tests -q" in contributing
    assert "scripts\\minify_static_assets.py" in contributing
    assert "提交信息使用中文" in contributing
    assert "数据模型" in architecture
    assert "消息入库流程" in architecture
    assert "离线可用性" in architecture
    assert "CI 使用 `.forgejo/workflows/ci.yml`" in architecture
    assert "CONTRIBUTING.md" in readme
    assert "ARCHITECTURE.md" in readme


def test_forgejo_ci_workflow_runs_tests_and_docker_build():
    workflow = yaml.safe_load((ROOT / ".forgejo/workflows/ci.yml").read_text(encoding="utf-8"))

    assert workflow["name"] == "CI"
    test_job = workflow["jobs"]["test"]
    assert test_job["runs-on"] == "ubuntu-latest"
    assert test_job["services"]["postgres"]["image"] == "postgres:16-alpine"
    assert "pg_isready" in test_job["services"]["postgres"]["options"]
    steps = {step["name"]: step["run"] for step in test_job["steps"] if "run" in step}
    assert "python -m pip install -r requirements-dev.txt" in steps["Install dependencies"]
    assert "python scripts/minify_static_assets.py" in steps["Verify minified frontend assets"]
    assert "git diff --exit-code app/static/assets/app.min.css app/static/assets/app.min.js" in steps["Verify minified frontend assets"]
    assert steps["Compile Python modules"] == "python -m compileall app tests"
    assert steps["Run tests"] == "python -m pytest tests -q"
    assert steps["Build Docker image"] == "docker build -t chat-audit-core:ci ."


def test_wechat_tray_packaging_scripts_are_present_and_headless():
    build_script = (ROOT / "scripts/build_wechat_tray.ps1").read_text(encoding="utf-8")
    installer_script = (ROOT / "scripts/build_wechat_tray_installer.ps1").read_text(encoding="utf-8")
    install_script = (ROOT / "scripts/install_wechat_tray_startup.ps1").read_text(encoding="utf-8")
    uninstall_script = (ROOT / "scripts/uninstall_wechat_tray_startup.ps1").read_text(encoding="utf-8")
    config_script = (ROOT / "scripts/write_wechat_tray_config.ps1").read_text(encoding="utf-8")
    requirements = (ROOT / "wechat_tray_adapter/requirements.txt").read_text(encoding="utf-8")

    assert "--noconsole" in build_script
    assert "wechat_tray_adapter\\__main__.py" in build_script
    assert "--add-data $WcfData" in build_script
    assert "--hidden-import wcferry.client" in build_script
    assert "--hidden-import pynng" in build_script
    assert "wechat_tray_adapter.version" in build_script
    assert "Get-FileHash -Algorithm SHA256" in build_script
    assert "manifest.json" in build_script
    assert "payload.zip" in installer_script
    assert "ChatAuditWechatTraySetup.ps1" in installer_script
    assert "self_extracting_powershell" in installer_script
    assert "FromBase64String" in installer_script
    assert "HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Run" in installer_script
    assert "replace-with-operator-token" in installer_script
    assert "HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Run" in install_script
    assert "Set-ItemProperty" in install_script
    assert "Remove-ItemProperty" in uninstall_script
    assert "replace-with-operator-token" not in config_script
    assert "CHAT_AUDIT_TOKEN" not in config_script
    assert {"pystray", "Pillow", "wcferry", "pyinstaller"} <= set(requirements.splitlines())


def test_wechat_tray_adapter_has_package_version():
    version_file = (ROOT / "wechat_tray_adapter/version.py").read_text(encoding="utf-8")

    assert '__version__ = "0.1.0"' in version_file
