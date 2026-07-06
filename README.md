# QQ社交资产审计平台

QQ社交资产审计平台是一套面向 QQ / NapCat / OneBot 11 的本地化聊天资产备份与审计系统。它会把机器人账号可见的群聊、私聊、媒体、合并转发、卡片链接和会话资料沉淀到自己的数据库与本地文件存储中，方便长期留存、检索、导出、离线查看和审计。

项目优先保证三件事：

- **不串线**：适配器和机器人身份分离，同一个适配器切换到不同 QQ 账号时会自动识别并绑定对应身份档案。
- **可离线**：图片、语音、视频、文件、头像、群名称、卡片快照、合并转发内容等资产尽量缓存到本地，历史记录在断网后仍可查看。
- **可审计**：提供多账号视角、消息检索、黑白名单抓取策略、导出导入、自动备份、离线缺失检查和修复入口。

## 当前能力

- QQ / NapCat / OneBot 11 反向 WebSocket 接入。
- 多适配器、多机器人账号、多会话统一管理。
- 群聊和私聊消息入库，按机器人账号视角隔离查看。
- 文本、链接、图片、动画表情、语音、视频、普通文件、卡片消息、回复消息、戳一戳、合并转发消息解析与展示。
- 合并转发消息缓存与弹层预览，支持嵌套合并转发读取。
- 图片弹层预览，支持滚轮缩放、拖拽移动和双击复位。
- B 站、小程序等卡片优先解析真实网页链接，前端自动识别蓝链。
- 本地媒体缓存，内容哈希去重，避免重复落盘。
- 头像、群名称、群号、私聊档案本地缓存。
- 黑名单 / 白名单抓取策略，按群或个人配置文本、图片、语音、文件等抓取范围。
- 高级导出、导入预校验、导入恢复、自动备份和备份签名校验。
- 离线资产审计与自动修复，检查缺失头像、媒体、卡片和转发缓存。
- Web 控制台，支持浅色 / 深色主题、调色盘、操作记录、适配器管理和账号设置。
- 管理 API Token、数据库用户、角色权限、CSRF 防护、审计日志和基础 Prometheus 指标。

微信 PC 采集器相关代码仍保留在 `wechat_tray_adapter/`，当前属于可选实验方向。稳定版主线以 QQ / NapCat 消息备份与审计为核心。

## 技术栈

- 后端：FastAPI、SQLAlchemy Async、Pydantic Settings
- 数据库：PostgreSQL 16，开发环境可使用 SQLite
- 前端：原生 HTML / CSS / JavaScript，无外部 CDN
- 媒体：HTTPX 下载、内容 MD5 去重、可选 FFmpeg 转码
- 部署：Docker Compose，支持内置静态 FFmpeg 镜像
- 自动化：pytest、Alembic 迁移、Forgejo Actions CI

## 快速部署

1. 复制环境变量模板：

```bash
cp .env.example .env
```

2. 修改 `.env` 中的生产配置：

```text
APP_ENV=production
APP_SECRET_KEY=请替换为长随机字符串
ADMIN_API_TOKEN=请替换为管理后台 token
ONEBOT_ACCESS_TOKEN=请替换为 OneBot 连接 token
POSTGRES_PASSWORD=请替换为数据库密码
SYSTEM_INSTANCE_ID=你的实例名称
```

3. 启动服务：

```bash
docker compose up -d --build
```

4. 打开控制台：

```text
http://服务器IP:8000/
```

健康检查：

```text
http://服务器IP:8000/health
```

接口文档：

```text
http://服务器IP:8000/docs
```

## FFmpeg 可选方案

默认镜像不强制依赖系统 FFmpeg。需要语音 / 视频转码时推荐使用内置静态 FFmpeg 构建：

```bash
docker compose -f docker-compose.yml -f docker-compose.ffmpeg.yml up -d --build
```

如果宿主机已经有兼容的 FFmpeg，也可以使用挂载方案：

```bash
FFMPEG_HOST_BIN=/usr/bin/ffmpeg \
FFMPEG_HOST_LIB64=/lib64 \
FFMPEG_HOST_USR_LIB=/usr/lib \
docker compose -f docker-compose.yml -f docker-compose.ffmpeg-host.yml up -d
```

运行后可通过以下接口确认 FFmpeg 状态：

```text
GET /api/system/runtime
```

## NapCat 接入

平台提供 OneBot 11 反向 WebSocket 入口：

```text
ws://服务器IP:8000/onebot/v11/ws?adapter_id=napcat1&access_token=你的ONEBOT_ACCESS_TOKEN
```

推荐每个 NapCat 容器使用独立 `adapter_id`，例如：

```text
napcat1
napcat2
napcat3
```

适配器第一次连接后，系统会读取当前 QQ 账号身份并创建机器人身份档案。之后如果同一个适配器切换到另一个 QQ 账号，系统会识别新身份并建立新的档案，避免新旧机器人消息混合。

## 数据目录

运行数据默认挂载在：

```text
data/storage   # 媒体、头像、卡片快照、合并转发缓存等资产
data/backups   # 自动备份与失败记录
postgres_data  # Docker volume，PostgreSQL 数据库
```

稳定版源码包只保留 `data/storage/.gitkeep` 和 `data/backups/.gitkeep`，不会携带真实聊天记录、媒体文件、SQLite 数据库、日志或本地 `.env`。

## 自动备份与导入导出

默认每天 03:00 执行自动备份：

```text
AUTO_BACKUP_CRON=0 3 * * *
AUTO_BACKUP_KEEP_LATEST=7
```

可在 Web 控制台的设置页面直接修改备份计划，也可以使用 API：

```text
GET   /api/backup/status
PATCH /api/backup/settings
POST  /api/backup/run
GET   /api/export
POST  /api/import/validate
POST  /api/import
```

导出包包含 manifest、签名、消息、会话、身份档案和可携带的媒体资产。导入前会先执行校验，报告新增、更新、未变化、缺失媒体和校验错误。

## 抓取策略

每个机器人身份可配置黑名单 / 白名单：

- 黑白名单都为空：默认抓取所有可见会话。
- 黑名单存在目标：跳过这些群或个人。
- 白名单存在目标：只抓取白名单中的群或个人。

每个目标可独立配置抓取内容：

- 文本：包括普通文本、链接、卡片消息、合并转发。
- 图片：包括普通图片和动画表情。
- 语音。
- 文件：指 zip、安装包、文档等普通文件，默认关闭。

## 权限与安全

生产环境必须设置非默认密钥和 token：

```text
APP_SECRET_KEY=长随机字符串
ADMIN_API_TOKEN=管理 token
ONEBOT_ACCESS_TOKEN=OneBot 连接 token
```

管理接口支持三种角色：

- `viewer`：只读查询、搜索、审计日志和导入包预校验。
- `operator`：包含只读权限，可执行备份、离线修复、媒体回填和适配器更新。
- `admin`：最高权限，可执行删除、导入、用户管理和 token 管理。

支持数据库托管 Token：

```text
GET    /api/admin/tokens
POST   /api/admin/tokens
POST   /api/admin/tokens/{id}/rotate
DELETE /api/admin/tokens/{id}
```

支持 Web 登录用户：

```text
POST /api/auth/login
GET  /api/auth/me
POST /api/auth/logout
```

## 本地开发

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
copy .env.example .env
uvicorn app.main:app --reload --host 127.0.0.1 --port 8001
```

运行测试：

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
```

同步压缩前端资源：

```powershell
.\.venv\Scripts\python.exe scripts\minify_static_assets.py
```

## 发布包内容

稳定版发布包包含运行所需文件：

- `app/`
- `migrations/`
- `data/storage/.gitkeep`
- `data/backups/.gitkeep`
- `vendor/wheels/`
- `Dockerfile*`
- `docker-compose*.yml`
- `requirements*.txt`
- `alembic.ini`
- `scripts/`
- `wechat_tray_adapter/`
- `README.md`、`ARCHITECTURE.md`、`CONTRIBUTING.md`、`DISASTER_RECOVERY.md`

发布包会排除：

- `.env`、真实 token、密钥和证书
- SQLite 数据库、媒体缓存、备份文件和日志
- `.venv`、`.tmp`、`build`、`dist`、`__pycache__`、pytest 缓存
- 测试目录、CI 配置、早期审计报告和开发队列文档

## 项目状态

当前主线已进入第一个稳定版：QQ / NapCat 消息备份、资产缓存、审计查看、导入导出和基础运维功能已经闭环。后续版本会继续围绕稳定性、移动端适配、更多消息类型细节和采集器生态扩展推进。
