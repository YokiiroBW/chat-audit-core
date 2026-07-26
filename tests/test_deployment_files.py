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


def test_docker_compose_defaults_to_sqlite_and_app_healthcheck():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))

    services = compose["services"]
    assert set(services) == {"app"}
    # 端口保留可覆盖的绑定地址前缀，默认仍对所有网卡开放。
    assert any(entry.endswith("8000:8000") for entry in services["app"]["ports"])
    assert all(entry.startswith("${APP_BIND_ADDRESS:-0.0.0.0}:") for entry in services["app"]["ports"])
    assert "${DATABASE_DATA_PATH:-./data/database}:/app/data/database" in services["app"]["volumes"]
    assert "${MEDIA_STORAGE_PATH:-./data/storage}:/app/data/storage" in services["app"]["volumes"]
    assert "${BACKUP_STORAGE_PATH:-./data/backups}:/app/data/backups" in services["app"]["volumes"]
    assert services["app"]["networks"] == ["default", "qqbot_astrbot_network"]
    assert compose["networks"]["qqbot_astrbot_network"] == {"external": True}
    assert services["app"]["environment"]["DATABASE_URL"] == "${DATABASE_URL:-sqlite+aiosqlite:////app/data/database/chat_audit.sqlite3}"
    assert services["app"]["environment"]["APP_SECRET_KEY"] == "${APP_SECRET_KEY}"
    assert services["app"]["environment"]["SYSTEM_INSTANCE_ID"] == "${SYSTEM_INSTANCE_ID}"
    assert services["app"]["environment"]["ADMIN_API_TOKEN"] == "${ADMIN_API_TOKEN}"
    assert services["app"]["environment"]["ADMIN_API_TOKENS"] == "${ADMIN_API_TOKENS:-}"
    assert services["app"]["environment"]["ONEBOT_ACCESS_TOKEN"] == "${ONEBOT_ACCESS_TOKEN}"
    assert services["app"]["environment"]["LOG_LEVEL"] == "${LOG_LEVEL:-INFO}"
    assert services["app"]["environment"]["FFMPEG_LIBRARY_PATH"] == "${FFMPEG_LIBRARY_PATH:-}"
    assert services["app"]["environment"]["MEDIA_TRANSCODE_ENABLED"] == "${MEDIA_TRANSCODE_ENABLED:-false}"
    assert services["app"]["environment"]["STORAGE_ROOT"] == "/app/data/storage"
    assert services["app"]["environment"]["BACKUP_ROOT"] == "/app/data/backups"

    app_healthcheck = " ".join(services["app"]["healthcheck"]["test"])
    assert "python" in app_healthcheck
    assert "urllib.request" in app_healthcheck
    assert "sys.exit" in app_healthcheck
    assert "status" in app_healthcheck
    assert "curl" not in app_healthcheck
    assert services["app"]["healthcheck"]["timeout"] == "10s"
    assert services["app"]["healthcheck"]["retries"] == 5


def test_optional_postgres_compose_override_defines_database_service():
    compose = yaml.safe_load((ROOT / "docker-compose.postgres.yml").read_text(encoding="utf-8"))

    services = compose["services"]
    assert set(services) == {"app", "postgres"}
    assert services["app"]["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert services["app"]["environment"]["DATABASE_URL"] == "postgresql+asyncpg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@postgres:5432/${POSTGRES_DB}"
    assert services["postgres"]["environment"]["POSTGRES_PASSWORD"] == "${POSTGRES_PASSWORD}"
    assert "healthcheck" in services["postgres"]
    assert "postgres_data" in compose["volumes"]

    postgres_healthcheck = " ".join(services["postgres"]["healthcheck"]["test"])
    assert "pg_isready" in postgres_healthcheck
    assert "psql" in postgres_healthcheck
    assert "SELECT 1" in postgres_healthcheck
    assert services["postgres"]["healthcheck"]["start_period"] == "10s"


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
    assert "data/database/*" in dockerignore
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


def test_tracked_text_files_are_free_of_mojibake():
    # 一次 ASCII 编码写盘曾把多个文件里的 CJK 字符整片换成问号，且不可逆。
    # 这条断言与 CI 的 Verify text encoding 步骤同源，本地跑测试就能拦住复发。
    from scripts.check_text_encoding import check_text_encoding

    assert check_text_encoding() == []


def test_mojibake_guard_separates_damage_from_legitimate_question_marks():
    # 问号在源码里到处都是合法的，哨兵的价值全在这几条边界上，所以钉死它们。
    # 注意：本测试自身不能出现连续问号字面量，否则会被哨兵判为受损，故用 q 拼接。
    from scripts.check_text_encoding import _line_problems

    q = "?"
    legitimate = [
        ("app/x.js", f"const a = b {q}{q} c;"),
        ("app/x.js", f"const a = b {q}{q}= c;"),
        ("app/x.js", f"return qqNumber {q} 'a' : 'b';"),
        ("app/x.js", f"/[),.;:!{q}，。]+$/"),
        ("app/x.py", f'placeholders = ",".join("{q}" for _ in ids)'),
    ]
    for path, line in legitimate:
        assert _line_problems(path, line) == [], line

    damaged = [
        ("app/x.js", f"pushUiLog('{q * 5}' + name)"),
        ("collector/gui/app.py", f'title="{q * 2} QQ {q * 4}"'),
        # Markdown 散文可以长得和 JS 的空值合并运算符一模一样，所以豁免只对 .js 生效。
        ("README.md", f"QQNT 16 {q}{q} ASCII"),
    ]
    for path, line in damaged:
        assert _line_problems(path, line), line


def test_disaster_recovery_document_covers_restore_drill_and_targets():
    guide = (ROOT / "DISASTER_RECOVERY.md").read_text(encoding="utf-8")

    assert "RTO" in guide
    assert "RPO" in guide
    assert "AUTO_BACKUP_CRON" in guide
    assert "AUTO_BACKUP_KEEP_LATEST" in guide
    assert "/api/import/validate" in guide
    assert "/api/offline/audit" in guide
    assert "/api/offline/repair" in guide
    assert "演练流程" in guide


def test_forgejo_ci_workflow_runs_tests_and_docker_build():
    workflow = yaml.safe_load((ROOT / ".forgejo/workflows/ci.yml").read_text(encoding="utf-8"))

    assert workflow["name"] == "CI"
    test_job = workflow["jobs"]["test"]
    assert test_job["runs-on"] == "ubuntu-latest"
    assert test_job["services"]["postgres"]["image"] == "postgres:16-alpine"
    assert "pg_isready" in test_job["services"]["postgres"]["options"]
    steps = {step["name"]: step["run"] for step in test_job["steps"] if "run" in step}
    assert "python -m pip install -r requirements-dev.txt" in steps["Install dependencies"]
    # 乱码哨兵排在资产门禁之前，让编码损坏先报出来，而不是被误读成 min 产物过期。
    assert "python scripts/check_text_encoding.py" in steps["Verify text encoding"]
    step_names = [step["name"] for step in test_job["steps"]]
    assert step_names.index("Verify text encoding") < step_names.index("Verify minified frontend assets")
    # 资产门禁走 --check：只读比对、不写盘，因此后续步骤看到的仍是干净检出。
    assert "python scripts/minify_static_assets.py --check" in steps["Verify minified frontend assets"]
    assert "python -m pip check" in steps["Verify dependency resolution"]
    # pip-audit 必须装在独立 venv，否则它的依赖会扰动 requirements-prod.txt 钉定的版本。
    audit_step = steps["Audit production dependencies"]
    assert "pip-audit" in audit_step
    assert "requirements-prod.txt" in audit_step
    assert ".tmp/audit-venv" in audit_step
    assert steps["Compile Python modules"] == "python -m compileall app tests"
    assert steps["Run tests"] == "python -m pytest tests -q"
    assert steps["Build Docker image"] == "docker build -t chat-audit-core:ci ."
