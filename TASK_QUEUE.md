# 未完成任务队列

本文只记录仍需推进的事项。每个完成项都需要测试、中文提交、推送 Forgejo；涉及运行时或部署行为的变更还需要部署 NAS 并验收。

## 当前已完成摘要

- QQ/NapCat OneBot 接入、适配器与机器人身份档案分离、主视角隔离。
- QQ CQ 图片、动画表情、语音、视频、文件包/文档、卡片、合并转发、回复预览和本地缓存。
- 角色抓取策略：按机器人档案为群聊/私聊设置默认、黑名单、白名单，并按文字、图片/动画表情、语音、视频、文件包/文档分别控制。
- 卡片网页快照、头像、群资料缓存、离线审计和离线修复。
- 微信 Hook 通用入口和真实样本回放。
- 导出/导入包 checksum、系统签名、媒体嵌入。
- 自动备份状态、手动备份、数据库托管 cron/retention 配置。
- 审计日志、高风险限流、静态多角色 Token、数据库托管 Token。
- 数据库用户、登录态、退出、用户禁用、密码重置、会话列表、强制下线、Token 轮换。
- 轻量迁移注册表、`/api/system/migrations`、Alembic CLI、容器内 `alembic upgrade head` 验收。
- Forgejo SSH 专用 key 已生成并注册，SSH 推送可用。

## 可立即推进

### T8 交接文档持续更新

状态：持续项，本轮已更新到 `e03ba27` 之后。

目标：

- 每次版本推进后更新 `PROJECT_HANDOFF_READ_ME_FIRST.md` 的最新提交、测试数量、NAS 验收和剩余队列。
- 同步维护 `TASK_QUEUE.md` 和 `DEVELOPMENT_QUEUE.md`。

验收：

- 文档与当前主线一致。
- 不写入 token、密码、私钥或 NAS 敏感凭据。

## 需要外部条件

### T4 NAS 启用 FFmpeg 转码

状态：compose 可选路径已完成，实际启用待确认。

待确认：

- 若 NAS/宿主机已有 FFmpeg，设置 `FFMPEG_HOST_BIN` 并使用 `docker-compose.ffmpeg-host.yml` 挂载。
- 若 NAS/宿主机没有 FFmpeg，使用 `docker-compose.ffmpeg.yml` 自动构建内置 FFmpeg 镜像；该路径依赖 apt 源可用。

当前可用能力：

- `Dockerfile.ffmpeg`
- `docker-compose.ffmpeg.yml`
- `docker-compose.ffmpeg-host.yml`
- `/api/system/runtime`

验收：

- NAS 使用宿主机挂载或内置 FFmpeg 镜像启动。
- `/api/system/runtime` 返回 `ffmpeg_available=true`。
- 语音/视频转码样本验收通过。

### T9 微信 Hook 专用映射

状态：通用入口已完成，等待最终选定真实客户端后继续增强。

目标：

- 根据最终选定的微信 Hook 客户端补充专属字段映射。
- 增加真实样本回放。
- 更新部署说明。

验收：

- 新客户端样本可稳定归一化为内部消息模型。
- 不影响已有 QQ 和通用微信 Hook 流程。
