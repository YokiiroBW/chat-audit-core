# QQ & 微信多租户社交资产审计系统

本仓库用于落地 `QQ & 微信多租户社交资产审计系统 —— 全栈工程落地蓝图 (V4 架构).md`。

## 当前蓝图核心

- 主视角隔离：同一条群消息可被多个机器人账号看到，但查询时按 `robot_id` 做视角切片。
- 全局消息池去重：以 `msg_hash = MD5(platform + room_id + sender_id + raw_message)` 写入全局消息池。
- 内容寻址媒体存储：媒体文件以内容 MD5 命名并复用，避免重复落盘。
- 游标滚动加载：聊天历史使用 `before_timestamp + limit` 向上滚动加载，不做传统页码分页。
- 第一阶段优先打通 QQ/NapCat OneBot 11 反向 WebSocket 存储管道，微信作为第二阶段兼容扩展。

## 已落地能力

- FastAPI 应用工厂与启动初始化。
- SQLAlchemy Async V4 数据模型。
- 全局消息池去重与 `robot_id` 主视角绑定。
- `/api/adapters`、`/api/rooms`、`/api/messages` 主视角查询 API。
- `/onebot/v11/ws` NapCat / OneBot 11 反向 WebSocket 入库。
- CQ 图片、语音、视频解析、下载、内容 MD5 去重落盘。
- `/static/storage` 本地媒体静态访问。
- Dockerfile + Docker Compose 部署基座。

## 技术栈

- 后端：FastAPI + SQLAlchemy 2.x Async + Pydantic Settings
- 数据库：PostgreSQL（部署默认），SQLite（本地快速测试）
- 媒体：HTTPX 下载 + FFmpeg 运行时预装
- 部署：Dockerfile + Docker Compose，挂载 `data/storage` 与 `data/backups`

## 本地开发启动

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

健康检查：

```text
http://127.0.0.1:8000/health
```

接口文档：

```text
http://127.0.0.1:8000/docs
```

## Docker 部署

当前 Windows 环境 Docker CLI 可用，但未安装 `docker compose` 子命令；如果目标机器有 Docker Compose v2，可直接运行：

```powershell
docker compose up -d --build
```

如果目标机器使用旧版独立命令：

```powershell
docker-compose up -d --build
```

部署后访问：

```text
http://宿主机IP:8000/health
http://宿主机IP:8000/docs
```

NapCat 反向 WebSocket 配置为：

```text
ws://宿主机IP:8000/onebot/v11/ws
```

持久化目录：

```text
data/storage  # 内容寻址媒体池
data/backups  # 后续自动备份归档
```

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
```

## 媒体转码

默认情况下，系统会下载并缓存 QQ/NapCat 提供的原始媒体文件，保证原始记录可追溯。

如需提高语音、视频在浏览器里的播放兼容性，可启用 FFmpeg 转码：

```text
MEDIA_TRANSCODE_ENABLED=true
MEDIA_TRANSCODE_VOICE_EXT=mp3
MEDIA_TRANSCODE_VIDEO_EXT=mp4
FFMPEG_BIN=ffmpeg
FFMPEG_LIBRARY_PATH=
```

说明：

- 语音会尝试转为 MP3，视频会尝试转为 MP4。
- 转码失败或 FFmpeg 不可用时，会自动回退保存原始文件。
- 默认 Docker 镜像保持离线友好，不在构建期联网安装 FFmpeg；需要转码时可以选择挂载宿主机已有 FFmpeg，或自动构建内置 FFmpeg 镜像。

宿主机/NAS 已经有 FFmpeg 时，可直接挂载可执行文件到容器：

```powershell
$env:FFMPEG_HOST_BIN='/usr/bin/ffmpeg'
$env:FFMPEG_HOST_LIB64='/lib64'
$env:FFMPEG_HOST_USR_LIB='/usr/lib'
docker compose -f docker-compose.yml -f docker-compose.ffmpeg-host.yml up -d
```

`docker-compose.ffmpeg-host.yml` 会把宿主机 FFmpeg 二进制挂载到 `/opt/host-bin/ffmpeg`，并把宿主机库目录挂载到 `/opt/host-lib64` 与 `/opt/host-usr-lib`。应用只会在调用 FFmpeg 子进程时注入 `FFMPEG_LIBRARY_PATH`，不会把宿主机库路径设为整个 Python 服务的全局 `LD_LIBRARY_PATH`。

宿主机没有 FFmpeg，且部署环境允许联网 apt 构建时，可自动构建内置 FFmpeg 镜像：

```powershell
docker compose -f docker-compose.yml -f docker-compose.ffmpeg.yml up -d --build
```

运行时状态查询：

```text
GET /api/system/runtime
```

该接口会返回 `media_transcode_enabled`、`ffmpeg_bin`、`ffmpeg_available`、`ffmpeg_version` 等字段，用于确认容器内 FFmpeg 是否可用。

## 微信 Hook 接入

系统提供通用微信事件接收入口：

```text
POST /api/wechat/events
```

该接口复用管理 API 鉴权，支持常见 Hook 字段名，例如：

- 机器人账号：`robot_id`、`self_id`、`wxid`、`account_id`、`CurrentWxid`
- 会话：`room_id`、`talker`、`conversation_id`、`from_wxid`、`FromUserName`、`ToUserName`
- 发送者：`sender_id`、`sender_wxid`、`from_user`、`SenderWxid`
- 内容：`raw_message`、`content`、`Content`、`text`
- 媒体：`msg_type=image/voice/video/file` 搭配 `media_url`、`file_url`、`url`、`FileUrl`
- 常见嵌套：字段可以位于顶层，也可以位于 `data`、`payload`、`msg`、`message` 对象内。
- 常见数字类型：`MsgType=1` 文本、`3` 图片、`34` 语音、`43` 视频、`47` 表情、`49` 卡片/分享。

微信事件会被规范化为内部消息模型并以 `platform=wechat` 入库；图片、语音、视频和文件会转成现有 CQ 片段，继续复用本地媒体缓存、导出导入和离线验收链路。
群聊文本如果带有 `sender_wxid:\n内容` 前缀，会自动拆出真实发送者并去掉前缀后入库。

微信 Hook 样本回放：

```text
tests/fixtures/wechat_hook_samples.json
```

新增真实客户端样本时，优先追加到该文件并运行 `tests/test_wechat_pc_adapter.py`。

## 自动备份

应用启动时会根据 `AUTO_BACKUP_CRON` 启动自动备份任务，将导出包写入 `BACKUP_ROOT`。

当前支持每日固定时间格式：

```text
AUTO_BACKUP_CRON=0 3 * * *
AUTO_BACKUP_KEEP_LATEST=7
```

输出文件示例：

```text
data/backups/auto-backup-20260703T030000Z.json
```

说明：

- 备份内容复用 `/api/export` 的包结构。
- 导出包会尽量携带本地媒体文件内容，单个媒体文件超过 `MEDIA_MAX_BYTES` 时只导出索引与校验信息，不嵌入文件内容。
- manifest 会标记 `backup_type=auto` 与 `created_by=auto_backup_scheduler`。
- manifest 会写入 `checksum.algorithm=sha256` 与 `checksum.value`；导入时如校验值不匹配会拒绝导入，避免篡改包被静默回录。
- manifest 会写入 `source.system`、`source.instance_id` 与 `signature`；签名使用 `APP_SECRET_KEY` 做 HMAC-SHA256。旧版无签名包仍可导入，但校验报告会显示 signature 未提供。
- `/api/import/validate` 可在真正写库前返回 schema、checksum、counts、数据库差异预览、媒体文件校验结果与错误列表。
- Web 控制台支持“导入 JSON”：先校验导出包，通过后再确认导入；校验报告会展示新增/更新/不变统计与媒体文件 checked/missing/mismatch。
- 导入失败与自动备份失败会写入 `BACKUP_ROOT/failures.log`，每行一条 JSON 失败记录。
- `AUTO_BACKUP_KEEP_LATEST` 控制保留最近多少个 `auto-backup-*.json` 文件。
- 如需禁用自动备份，可将 `AUTO_BACKUP_CRON` 设置为 `off`、`disabled`、`none`、`false` 或 `0`。
- `.env` 仍作为默认配置；通过 Web 控制台或 `PATCH /api/backup/settings` 保存的数据库覆盖项优先生效，不会回写 `.env`。

管理接口：

```text
GET   /api/backup/status    # 查看自动备份开关、cron、保留数量、配置来源和最新备份
PATCH /api/backup/settings  # 更新 cron/保留数量，或 {"reset_to_env": true} 恢复 .env 默认值
POST  /api/backup/run       # 立即执行一次签名自动备份
```

Web 控制台的账号设置面板提供自动备份状态、cron/保留数量编辑、恢复 `.env` 默认值与“立即备份”按钮。配置变更会写入 `audit_logs`。

## 管理 API 鉴权

开发环境中 `ADMIN_API_TOKEN` 留空时，`/api/*` 管理接口默认开放，便于本地调试。

生产环境 `APP_ENV=production` 时必须配置非默认 `ADMIN_API_TOKEN`，或配置 `ADMIN_API_TOKENS` 角色 Token。配置后，请求 `/api/*` 需要携带：

```text
Authorization: Bearer 你的管理API Token
```

或：

```text
X-Admin-Token: 你的管理API Token
```

内置 Web 控制台遇到 401 时会提示输入该 token，并缓存在浏览器本地存储中。

`ADMIN_API_TOKEN` 为兼容旧部署的最高权限 token。需要多角色时可额外配置 `ADMIN_API_TOKENS` JSON：

```text
ADMIN_API_TOKENS=[{"name":"readonly","role":"viewer","token":"replace-with-readonly-token"}]
```

角色说明：

- `viewer`：只读查询、搜索、审计日志、导入包预校验。
- `operator`：包含只读权限，并可执行媒体回填、离线修复、手动备份、适配器创建/更新。
- `admin`：最高权限，可删除适配器、执行导入等破坏性写操作。

也可以使用数据库托管 Token 做日常分权和轮换。托管 Token 只保存 SHA-256 哈希，完整 token 仅在创建成功时返回一次：

```text
GET    /api/admin/tokens
POST   /api/admin/tokens       # body: {"name":"readonly","role":"viewer"}
POST   /api/admin/tokens/{id}/rotate
DELETE /api/admin/tokens/{id}  # 吊销
```

数据库用户与登录态也可用于 Web 控制台日常登录。密码使用 PBKDF2-SHA256 保存，登录态只保存 token 哈希：

```text
POST /api/auth/login       # body: {"username":"ops","password":"..."}
GET  /api/auth/me
POST /api/auth/logout
GET  /api/admin/users
POST /api/admin/users      # body: {"username":"ops","password":"...","role":"operator"}
POST /api/admin/users/{id}/password  # body: {"password":"new-password"}
DELETE /api/admin/users/{id}
GET  /api/admin/sessions
DELETE /api/admin/sessions/{id}
```

生产环境建议仍保留一个静态 `ADMIN_API_TOKEN` 作为 bootstrap/应急入口，再用数据库托管 Token 分配日常只读或运维权限。
Web 控制台的账号设置面板提供数据库托管 Token 的列表、创建、吊销入口，以及数据库用户创建、列表、禁用、密码重置、会话列表、强制下线、登录、退出和当前角色显示；高风险控件会按当前角色禁用。

## 操作审计与限流

系统会将高风险管理操作写入 `audit_logs`，包括：

- 管理 API 鉴权失败。
- 删除适配器。
- 媒体回填。
- 离线修复。
- 导入 JSON。
- 手动备份。
- 用户登录/退出、数据库用户创建/禁用/密码重置、数据库会话强制下线、Token 创建/轮换/吊销。

查询接口：

```text
GET /api/audit/logs?action=offline.repair&limit=100
```

高风险写操作有简单每分钟限流，默认：

```text
HIGH_RISK_RATE_LIMIT_PER_MINUTE=10
```

设置为 `0` 可关闭该限流。

## 数据库迁移记录

应用启动时会执行 `create_all` 与轻量兼容迁移，并将已确认的兼容迁移写入 `schema_migrations`。当前迁移记录覆盖：

- `adapters.current_robot_id`
- `messages.external_message_id`
- `audit_logs`
- `schema_migrations`
- `admin_tokens`
- `system_settings`
- `admin_users`
- `admin_sessions`

轻量启动迁移仍会在应用启动时做兼容兜底；同时已启用 Alembic CLI，`migrations/versions/` 与轻量迁移注册表一一对应，空库可初始化为当前 schema，旧库可补齐已知兼容列。

本地或部署环境手动迁移：

```powershell
$env:DATABASE_URL='sqlite+aiosqlite:///./data/chat_audit.sqlite3'
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m alembic current
```

容器内可使用同样的 `DATABASE_URL` 执行 `python -m alembic upgrade head`。

迁移状态查询：

```text
GET /api/system/migrations
```

## Forgejo

局域网仓库：

```text
http://192.168.31.210:18085/YokiiroBW/chat-audit-core
```

## 仓库连接检查

新增检查脚本（双通道）：`scripts/git_connectivity_check.py`

- SSH 与 token 两条链路都已具备检测：
  - SSH：`python3 scripts/git_connectivity_check.py --remote origin`，通过私钥与主机密钥握手 + `git ls-remote`
  - HTTPS + token：同命令会自动读取 Forgejo token 并做 API + git 授权校验

常用命令（按环境覆盖）：
```bash
# 全量检查
FORGEJO_TOKEN_FILE=/path/to/forgejo.token python3 scripts/git_connectivity_check.py --remote origin

# 仅 HTTPS（当 SSH 尚未就绪）
python3 scripts/git_connectivity_check.py --remote origin --skip-ssh

# 仅 SSH（当 token 不可用）
python3 scripts/git_connectivity_check.py --remote origin --skip-https

# 跳过 SSH 口令/私钥错误导致阻塞时
python3 scripts/git_connectivity_check.py --remote origin --skip-ssh
```

建议执行顺序：先执行 HTTPS（确认 token 与 API/仓库可达），再修复 SSH Key 后补跑 `--skip-https`。
