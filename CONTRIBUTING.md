# 贡献指南

感谢参与 chat-audit-core 的开发。这个项目以 QQ/NapCat 消息备份、离线可用性和审计体验为当前主线；微信路线已封存，除非重新决策，不作为默认开发目标。

## 开发环境

### 要求

- Python 3.11+
- Git
- Docker / Docker Compose，用于生产模拟和镜像构建
- PostgreSQL 16+，生产部署使用；本地单元测试可使用 SQLite

### 本地启动

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
copy .env.example .env
uvicorn app.main:app --reload --host 127.0.0.1 --port 8001
```

访问：

```text
http://127.0.0.1:8001/
```

### 测试

Windows 下建议把 pytest 临时目录放在仓库内：

```powershell
New-Item -ItemType Directory -Force .tmp_pytest | Out-Null
$env:TEMP=(Resolve-Path .tmp_pytest).Path
$env:TMP=(Resolve-Path .tmp_pytest).Path
.\.venv\Scripts\python.exe -m pytest tests -q
```

前端资源改动后需要重新生成压缩产物：

```powershell
.\.venv\Scripts\python.exe scripts\minify_static_assets.py
node --check app\static\assets\app.min.js
node --check app\static\sw.js
```

### Docker 验证

```bash
docker compose up -d --build
docker compose ps
curl http://127.0.0.1:8000/health
```

需要内置 FFmpeg 时使用：

```bash
docker compose -f docker-compose.yml -f docker-compose.ffmpeg.yml up -d --build
```

## 分支、提交与推送

- 默认在 `main` 上推进当前单人维护流；多人协作时使用短分支名，例如 `feature/offline-audit`.
- 提交信息使用中文，格式建议为 `类型：一句话说明`，例如 `修复：完善头像缓存回填`。
- 每个可验收项单独提交，并推送到 Forgejo。
- 不要提交 `.env`、token、私钥、NAS 凭据、临时脚本输出和真实用户敏感数据。

## 代码规范

- Python 使用清晰的类型标注和小范围函数；优先复用现有 service 层。
- 数据库 schema 变更必须同步更新 `app/models.py`、轻量迁移注册表和 Alembic 版本脚本。
- API 写操作需要考虑鉴权、审计日志、限流和测试。
- 媒体缓存逻辑必须保持“失败降级不阻断消息入库”。
- 前端为原生 HTML/CSS/JS，新增交互要同时更新 `app.js` 与 `app.min.js`。
- 静态资源路径默认使用 `/assets/*.min.*`，源码文件保留给开发审查。

## 测试要求

按变更风险选择测试范围：

- API/service 逻辑：运行对应测试文件和全量测试。
- 数据库迁移：运行 `tests/test_alembic_cli.py`、`tests/test_migration_versions.py`、`tests/test_migration_rollback.py`。
- 前端资源：运行 `tests/test_web_console.py`，并执行 `node --check`。
- 部署文件：运行 `tests/test_deployment_files.py`。
- 离线缓存/媒体/备份：至少运行相关 service 测试和全量测试。

## 评审清单

提交前确认：

- `pytest tests -q` 通过。
- 没有未清理的 `.tmp*` 目录或本地数据文件。
- 新配置已写入 `.env.example` 或 README。
- 新文档没有泄露 token、密码、私钥、QQ 个人隐私或 NAS 凭据。
- 需要压缩的前端资源已重新生成。
- 需要部署的变更已说明是否需要 NAS 验收。

## 故障处理

- 本地测试临时目录权限异常：设置 `TEMP` 和 `TMP` 到仓库内 `.tmp_pytest`。
- PostgreSQL 连接失败：先检查 `docker compose ps` 与 `DATABASE_URL`。
- 前端页面仍加载旧资源：浏览器清 Service Worker 缓存，或更新 `app/static/sw.js` 的 `CACHE_NAME`。
- 媒体无法显示：运行 `/api/offline/audit`，再按报告决定回填或生成不可恢复占位。
