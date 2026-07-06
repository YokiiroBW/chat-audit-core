# 架构设计

## 目标

chat-audit-core 是一个社交消息资产审计系统，当前主线是 QQ/NapCat OneBot 11 消息备份、媒体本地化、离线验收和审计查询。核心目标是：即使断网，已缓存的历史消息、头像、群资料、图片、语音、视频、文件、卡片和合并转发仍可在本地查看。

## 模块分层

```text
NapCat / OneBot
  -> app/ws.py
  -> app/adapters/
  -> app/services/
  -> app/models.py
  -> PostgreSQL / SQLite
  -> data/storage
  -> app/static
```

- `app/ws.py`：OneBot 反向 WebSocket、连接注册、心跳、后台资料/合并转发缓存。
- `app/api.py`：HTTP API、管理鉴权、审计日志、限流、导入导出、离线审计。
- `app/services/message_service.py`：消息入库、全局去重、机器人视角绑定。
- `app/services/media_service.py`：CQ 媒体下载、本地化、卡片快照、合并转发缓存。
- `app/services/query_service.py`：房间、消息、搜索和回复上下文查询。
- `app/services/backup_service.py`：导出、导入、checksum、signature、自动备份。
- `app/static/`：无框架 Web 控制台、压缩资源、Service Worker 离线前端壳。

## 数据模型

- `adapters`：协议连接配置，和实际机器人身份分离。
- `bot_profiles`：机器人身份档案，适配器连接到新机器人时自动建档。
- `messages`：全局消息池，按 `msg_hash` 去重。
- `robot_messages`：机器人视角表，同一全局消息可以绑定到多个机器人。
- `media_assets`：内容寻址媒体索引。
- `room_profiles` / `user_profiles`：群资料、私聊用户和头像缓存。
- `capture_target_policies`：黑白名单和内容类型抓取策略。
- `audit_logs`：高风险操作审计记录。
- `schema_migrations`：轻量迁移记录。

## 消息入库流程

```text
OneBot event
  -> normalize_message_event
  -> CapturePolicyService.should_capture
  -> MediaService.rewrite_cq_media_to_local_paths
  -> MessageService.process_incoming_message
  -> Message + RobotMessage
  -> 后台缓存头像、群资料、合并转发详情
```

设计要点：

- 适配器不再固定等同机器人身份；发现 `self_id` 后写入 `BotProfile` 并更新 `Adapter.current_robot_id`。
- 媒体下载失败不能阻断消息入库，必须保留原始消息并在离线审计中暴露缺失原因。
- 合并转发支持递归缓存深度限制，避免无限展开。
- 回复消息通过 `external_message_id` 查询上下文，前端可跳转定位。

## 离线可用性

离线验收由 `/api/offline/audit` 执行，检查：

- 远程媒体 URL 是否仍残留。
- 本地媒体索引是否缺失。
- 本地文件是否缺失。
- 卡片页面快照是否缺失。
- 合并转发 payload 是否未缓存。
- 头像和群资料是否未缓存或非本地路径。

修复入口为 `/api/offline/repair`。能恢复的资产会重新写入 `data/storage` 和 `media_assets`；不能恢复的资产会以明确原因报告，不再被误判为未知 bug。

## 备份恢复

自动备份由 `AUTO_BACKUP_CRON` 和 `AUTO_BACKUP_KEEP_LATEST` 控制。导出包包含 manifest、checksum、signature、消息、配置、资料缓存和可嵌入媒体。灾难恢复流程见 `DISASTER_RECOVERY.md`。

## 鉴权、审计和限流

- 静态 token：`ADMIN_API_TOKEN` 和 `ADMIN_API_TOKENS`。
- 数据库用户：`admin_users` 和 `admin_sessions`。
- 高风险操作写入 `audit_logs`。
- 高风险限流由 `HIGH_RISK_RATE_LIMIT_PER_MINUTE` 控制。
- `/metrics` 导出 HTTP、WebSocket、媒体下载和限流指标。

## 前端

前端使用原生 HTML/CSS/JS：

- 源码：`app/static/assets/app.js`、`app/static/assets/app.css`。
- 生产加载：`app.min.js`、`app.min.css`。
- 生成脚本：`scripts/minify_static_assets.py`。
- 离线前端壳：`app/static/sw.js` 缓存 `/` 和 minified assets。

## 部署

默认部署：

```bash
docker compose up -d --build
```

可选内置 FFmpeg：

```bash
docker compose -f docker-compose.yml -f docker-compose.ffmpeg.yml up -d --build
```

CI 使用 `.forgejo/workflows/ci.yml`，包含依赖安装、压缩资源一致性检查、Python 编译、全量测试和 Docker 镜像构建。
