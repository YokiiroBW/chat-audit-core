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
- manifest 会标记 `backup_type=auto` 与 `created_by=auto_backup_scheduler`。
- manifest 会写入 `checksum.algorithm=sha256` 与 `checksum.value`；导入时如校验值不匹配会拒绝导入，避免篡改包被静默回录。
- `AUTO_BACKUP_KEEP_LATEST` 控制保留最近多少个 `auto-backup-*.json` 文件。
- 如需禁用自动备份，可将 `AUTO_BACKUP_CRON` 设置为 `off`、`disabled`、`none`、`false` 或 `0`。

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
