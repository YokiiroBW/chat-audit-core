# QQ & 微信多租户社交资产审计系统

本仓库用于落地 `QQ & 微信多租户社交资产审计系统 —— 全栈工程落地蓝图 (V4 架构).md`。

## 当前蓝图核心

- 主视角隔离：同一条群消息可被多个机器人账号看到，但查询时按 `robot_id` 做视角切片。
- 全局消息池去重：以 `msg_hash = MD5(platform + room_id + sender_id + raw_message)` 写入全局消息池。
- 内容寻址媒体存储：媒体文件以内容 MD5 命名并复用，避免重复落盘。
- 游标滚动加载：聊天历史使用 `before_timestamp + limit` 向上滚动加载，不做传统页码分页。
- 第一阶段优先打通 QQ/NapCat OneBot 11 反向 WebSocket 存储管道，微信作为第二阶段兼容扩展。

## 推荐落地栈

- 后端：FastAPI + SQLAlchemy 2.x Async + Alembic + Pydantic Settings
- 数据库：PostgreSQL（默认），SQLite 仅用于本地快速测试
- 任务/下载：httpx/aiofiles，后续可扩展 APScheduler/RQ/Celery
- 前端：第一阶段先内置静态控制台；后续如 UI 复杂化再拆 Vite/Vue 工程
- 部署：Dockerfile + docker-compose，挂载 `data/storage` 与 `data/backups`

## 第一阶段目标

打通从 NapCatQQ 反向 WebSocket 收消息，到消息/媒体去重入库，再到 Web 控制台按机器人账号和群聊读取消息的闭环。

## 本地启动约定（待实现）

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

## Forgejo 推送准备

待用户提供局域网 Forgejo 地址和 Access Token 后，由 Hermes 创建仓库并推送。Token 不会写入 git remote 或提交文件。
