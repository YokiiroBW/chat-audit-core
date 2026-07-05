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
- 轻量迁移注册表和 `/api/system/migrations`。
- Forgejo SSH 专用 key 已生成并注册，SSH 仓库连通性检查通过。

## 可立即推进

### T7.1 Alembic 版本脚本骨架

状态：已完成

目标：
- 在不新增运行时依赖的前提下，先建立 Alembic 风格的迁移目录与版本脚本。
- 让当前轻量迁移注册表与版本脚本一一对应。
- 为后续真正启用 Alembic CLI 降低切换成本。

验收：
- 每个 `LIGHTWEIGHT_MIGRATION_REGISTRY` 版本都有对应 `migrations/versions/*.py`。
- 版本脚本链路顺序与轻量迁移注册表一致。
- 本地全量测试通过。

已完成：
- 新增 `migrations/versions/`，为当前 7 个轻量迁移建立 Alembic 风格版本脚本。
- 新增 `tests/test_migration_versions.py`，校验版本脚本与轻量迁移注册表一一对应。
- 未新增运行时依赖，NAS 部署不会因该步骤重新拉取 Alembic 包。

### T7.2 启用完整 Alembic CLI

状态：已完成

目标：
- 将 Alembic 加入依赖并提供 `alembic.ini`、`env.py` 和升级命令。
- 本地与 NAS 均可执行版本化迁移。

已完成：
- `requirements.txt` 新增 `alembic==1.16.5`。
- 新增 `alembic.ini` 与 `migrations/env.py`，读取当前 `DATABASE_URL` 执行迁移。
- 现有 7 个版本脚本已改为可执行幂等迁移。
- 新增 `tests/test_alembic_cli.py`，覆盖空库初始化和旧库兼容列补齐。
- 本地 `python -m alembic upgrade head` 与 `python -m alembic current` 已通过。

验收：
- 本地 `alembic upgrade head` 可用。
- NAS 部署不依赖手工改表。

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
