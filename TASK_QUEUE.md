# 未完成任务队列

本文件只记录仍需推进的事项，按可执行优先级排列。每个完成项都需要测试、中文提交、推送 Forgejo；涉及运行时的变更还要部署 NAS 并验收。

## 当前已完成摘要

- QQ/NapCat OneBot 接入、适配器与机器人身份分离、主视角隔离。
- QQ CQ 图片、语音、视频、文件、卡片、合并转发、回复预览和本地缓存。
- 卡片网页快照、头像/群资料缓存、离线审计和离线修复。
- 微信 Hook 通用入口和真实样本回放。
- 导出/导入包 checksum、系统签名、媒体嵌入。
- 自动备份状态、手动备份、数据库托管 cron/retention 配置。
- 审计日志、高风险限流、静态多角色 Token、数据库托管 Token。
- 数据库用户、登录态、退出、用户禁用、密码重置、会话列表、强制下线、Token 轮换。
- 轻量迁移注册表、`/api/system/migrations`、Alembic CLI、容器内 `alembic upgrade head` 验收。
- Forgejo SSH 专用 key 已生成并注册，SSH 仓库连通性检查通过。

## 可立即推进

### T8 交接文档持续更新

状态：持续项，本轮已更新

目标：
- 每次版本推进后更新 `PROJECT_HANDOFF_READ_ME_FIRST.md` 的最新提交、测试数量、NAS 验收和剩余队列。

验收：
- 文档与当前主线一致。
- 不写入 token、密码或 NAS 敏感凭据。

## 需要外部条件

### T4 NAS 启用容器内置 FFmpeg

状态：待外部确认

阻塞：
- 需要确认 NAS 构建环境可访问 apt 源，或提供内网预构建镜像。
- 2026-07-05 复查：内置 FFmpeg 方案仍依赖 `Dockerfile.ffmpeg` 中的 `apt-get install ffmpeg`。

当前可用能力：
- `Dockerfile.ffmpeg`
- `docker-compose.ffmpeg.yml`
- `/api/system/runtime`

验收：
- NAS 使用 FFmpeg 镜像启动。
- `/api/system/runtime` 返回 `ffmpeg_available=true`。
- 语音/视频转码样本验收通过。
