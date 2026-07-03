---
title: "QQWXTB / chat-audit-core 项目续作交接说明"
project: "QQ 与 微信多租户社交资产审计系统"
repo: "chat-audit-core"
forgejo: "http://192.168.31.210:18085/YokiiroBW/chat-audit-core"
branch: "main"
updated_at: "2026-07-03"
read_this_first: true
---

# READ ME FIRST：QQWXTB / chat-audit-core 项目续作交接说明

给后续对话或后续模型看的快速接手文件。目标：不用翻完整聊天记录，也能快速掌握当前仓库状态、已完成能力、验证命令和下一步推进顺序。

## 0. 当前项目一句话概况

本仓库正在落地“QQ 与 微信多租户社交资产审计系统 —— V4 架构”。

当前已经完成一个可运行、可测试、可 Docker 部署的 FastAPI 后端与内置 Web 控制台基础版本，核心闭环是：

```text
NapCat / OneBot 11 反向 WebSocket
  -> 归一化 QQ 消息
  -> 全局消息池去重
  -> robot_id 主视角绑定
  -> CQ 图片/语音/视频下载并本地内容寻址存储
  -> API 查询 / 搜索 / 导出导入
  -> Web 控制台按机器人视角查看会话与消息
  -> 自动备份写入 data/backups
```

## 1. 当前仓库状态

最近已推送到 Forgejo 的主线功能边界：

```text
0e5f52b docs: document repository checks and backups
c85746c feat: add automatic backup scheduler
6922370 feat: enable filtered export downloads in console
94d5e4e chore: add Forgejo connectivity check script
b3d850f chore: add NAS deployment helper scripts
```

已验证：

```text
32 passed, 41 warnings
origin/main 与本地 main：behind=0 ahead=0
HTTPS token 仓库链路：PASS
SSH 链路：端口可达，但 Forgejo Git SSH 鉴权尚未完成
```

注意：本文件提交后 HEAD 会更新；以上列表记录的是本文件更新前的功能提交边界。

## 2. 推荐读取顺序

1. `PROJECT_HANDOFF_READ_ME_FIRST.md`：当前文件。
2. `README.md`：运行方式、Docker/NapCat、仓库连接检查、自动备份说明。
3. `.hermes/plans/2026-07-02_192651-v4-implementation-roadmap.md`：原始阶段计划与推进策略。
4. `QQ 与 微信多租户社交资产审计系统 —— 全栈工程落地蓝图 (V4 架构).md`：原始 V4 架构蓝图。
5. 核心代码：`app/main.py`、`app/api.py`、`app/ws.py`、`app/models.py`、`app/services/*`、`app/static/index.html`。
6. 测试目录：`tests/`。

## 3. 已落地能力

### 3.1 工程基线

- FastAPI 应用工厂。
- SQLAlchemy Async engine/session。
- SQLite 本地测试 + PostgreSQL Docker 部署配置。
- Dockerfile + Docker Compose。
- `data/storage` 与 `data/backups` 持久化目录。

### 3.2 V4 数据模型

核心模型在 `app/models.py`：

- `Adapter`：协议/机器人账号配置表。
- `Message`：全局消息池，`msg_hash` 主键。
- `RobotMessage`：机器人主视角关联表，唯一约束 `robot_id + msg_hash`。
- `MediaAsset`：媒体内容寻址索引表。

### 3.3 消息入库与去重

`app/services/message_service.py` 已实现：

- `MessageService.generate_md5()`。
- `MessageService.process_incoming_message()`。
- 同一消息全局只入库一次。
- 同一消息可绑定多个机器人视角。
- 同一机器人重复看到同一消息不会重复绑定。

消息哈希规则：

```text
MD5(platform + room_id + sender_id + raw_message)
```

### 3.4 OneBot / NapCat 接入

- Endpoint：`/onebot/v11/ws`。
- 支持 `post_type=message` 与 `message_sent`。
- 支持群聊和私聊消息。
- 使用 `self_id` 作为 `robot_id`。
- 支持 OneBot WebSocket access token：查询参数 `access_token` 或 `Authorization: Bearer`。
- 媒体下载失败不会断开 WebSocket，会保留原始消息入库。

### 3.5 媒体解析与本地化

`app/services/media_service.py` 已支持：

- `CQ:image`、`CQ:record`、`CQ:video`。
- 从 CQ 参数提取 `url`。
- 下载后用内容 MD5 命名。
- 写入 `data/storage`。
- 写入 `media_assets` 索引。
- `local_message` 将 CQ 段替换为 `/static/storage/...`。

### 3.6 查询、搜索、导出导入 API

已实现：

```text
GET    /api/adapters
POST   /api/adapters
PATCH  /api/adapters/{adapter_id}
DELETE /api/adapters/{adapter_id}
GET    /api/rooms
GET    /api/messages
GET    /api/search
GET    /api/export
POST   /api/import
```

所有房间、消息、搜索均经过 `RobotMessage`，避免绕过主视角隔离。

### 3.7 内置 Web 控制台

`app/static/index.html` 已支持：

- 左侧机器人账号列表。
- 中间会话列表、搜索、账号设置。
- 右侧聊天消息视图。
- 切换机器人视角。
- 游标加载更早消息。
- 渲染本地图片/视频/语音。
- adapter 新增、编辑、删除。
- 高级过滤导出弹窗：`robot_id`、`room_id`、`start_timestamp`、`end_timestamp`。
- 下载 JSON 导出包，文件名以 `chat-audit-export-` 开头。

### 3.8 导出/导入与自动备份

`app/services/backup_service.py` 已支持：

- `BackupService.export_package()`。
- `BackupService.import_package()`。
- `BackupService.calculate_package_checksum()`。
- `BackupService.attach_package_checksum()`。
- `BackupService.validate_package_checksum()`。
- `BackupService.write_auto_backup_file()`。
- `BackupService.next_run_from_cron()`。
- `start_auto_backup_scheduler()`。

自动备份配置：

```text
AUTO_BACKUP_CRON=0 3 * * *
AUTO_BACKUP_KEEP_LATEST=7
```

当前 cron 支持每日固定时间格式，例如 `0 3 * * *`、`15 3 * * *`。

禁用值：`off` / `disabled` / `none` / `false` / `0`。

输出示例：

```text
data/backups/auto-backup-20260703T030000Z.json
```

导出包 manifest 已包含 SHA256 checksum；导入时如果 `manifest.checksum.value` 与包内容不匹配，会拒绝导入并报 `checksum mismatch`。

## 4. 仓库连接/鉴权状态

仓库连接检查脚本：

```text
scripts/git_connectivity_check.py
```

推荐验证命令：

```bash
python3 scripts/git_connectivity_check.py --remote origin --skip-ssh --reconcile
```

当前环境结论：

- HTTPS + token 可用。
- `origin/main...HEAD` 已对齐。
- SSH 端口 `2222` 可达，但 Git SSH 鉴权未完成；需要把当前环境公钥加入 Forgejo 账号 SSH Key 或仓库 Deploy Key 后再补跑：

```bash
python3 scripts/git_connectivity_check.py --remote origin --skip-https
```

安全要求：

- 不要把 token 写入文件、remote URL、Git config 或提交历史。
- 推送 Forgejo 时使用临时 HTTP header。
- `.env` 被忽略，只提交 `.env.example`。

## 5. 常用验证命令

全量测试：

```bash
uv run --python /usr/bin/python --with pytest --with pytest-asyncio --with httpx --with fastapi --with sqlalchemy --with aiosqlite --with pydantic-settings --with pyyaml python -m pytest tests -q
```

快速语法检查：

```bash
python3 -m py_compile scripts/git_connectivity_check.py app/services/backup_service.py app/main.py app/config.py
```

仓库连接检查：

```bash
python3 scripts/git_connectivity_check.py --remote origin --skip-ssh --reconcile
```

Git 状态：

```bash
git status --short
git log --oneline -5
git rev-list --left-right --count origin/main...HEAD
```

本地启动：

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

访问：

```text
http://127.0.0.1:8000/health
http://127.0.0.1:8000/
http://127.0.0.1:8000/docs
```

NapCat 反向 WebSocket：

```text
ws://宿主机IP:8000/onebot/v11/ws
```

如果启用了 token：

```text
ws://宿主机IP:8000/onebot/v11/ws?access_token=你的token
```

## 6. 未完成/建议下一步

### 6.1 处理 SSH 鉴权

- 生成或定位当前环境 Forgejo 专用公钥。
- 加入 Forgejo 账号 SSH Key 或仓库 Deploy Key。
- 复跑 `python3 scripts/git_connectivity_check.py --remote origin --skip-https`。
- 成功后可考虑把 `origin` 切回 SSH 长期维护路径。

### 6.2 生产安全加固

当前 compose 仍有示例值，建议：

- compose 改 `.env` 注入。
- 生产强制非默认 `APP_SECRET_KEY`。
- 生产建议强制 `ONEBOT_ACCESS_TOKEN`。
- 管理 API 增加鉴权。

### 6.3 备份增强

- 媒体文件校验。
- 导入前校验报告。
- 失败日志。
- 导入 UI。

### 6.4 NAS / NapCat 真机验收

- 确认 NAS 容器运行最新提交。
- 真实 NapCat 连接 `/onebot/v11/ws`。
- 真实群消息入库。
- 真实图片/语音/视频落盘。
- Web 控制台可见。
- 长时间运行稳定性观察。

### 6.5 微信 adapter

QQ/NapCat 真实闭环稳定后，再推进微信接入。

## 7. 关于 `uv.lock`

本项目当前依赖来源仍是 `requirements.txt`，`pyproject.toml` 只存 pytest 配置。当前生成的 `uv.lock` 不包含实际依赖锁定信息，因此不建议提交；如后续迁移到 uv 依赖管理，应先把依赖声明迁入 `pyproject.toml`，再提交有实际内容的 `uv.lock`。
