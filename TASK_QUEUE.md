# 未完成任务队列

本文件只记录仍需推进的事项，按可执行优先级排列。每个完成项都需要测试、中文提交、推送 Forgejo；涉及运行时的变更还要部署 NAS 并验收。

## 可立即推进

### T1 微信 Hook 真实样本回放

状态：已完成

目标：
- 将常见微信 Hook 样本固化为 fixtures。
- 使用样本回放测试覆盖文本、图片、语音、表情、分享卡片、群聊发送者前缀。
- 后续拿到真实客户端样本后，只需追加 fixture 即可回归。

验收：
- `tests/test_wechat_pc_adapter.py` 能读取样本并完成归一化断言。
- 全量测试通过。

已完成：
- 新增 `tests/fixtures/wechat_hook_samples.json`。
- 覆盖文本、图片、语音、表情、分享卡片和群聊发送者前缀。

### T2 自动备份配置持久化入口

状态：已完成

目标：
- 在不直接写 `.env` 的前提下，提供数据库托管的备份配置覆盖项。
- 支持查看/更新 cron 与保留数量。
- 与现有自动备份状态、手动备份和审计日志联动。

验收：
- 后端 API 与前端设置页覆盖。
- 配置变更写审计日志。
- 全量测试通过。

已完成：
- 新增 `system_settings` 轻量迁移和数据库托管覆盖项。
- `GET /api/backup/status` 返回有效配置与来源，`PATCH /api/backup/settings` 支持更新/恢复默认值。
- 手动备份与自动备份循环均使用数据库有效配置。
- Web 设置页支持编辑 cron/retention、恢复 `.env` 默认值，并覆盖测试。

### T3 轻量迁移体系增强

状态：待处理

目标：
- 将当前启动期兼容迁移整理为更清晰的迁移注册结构。
- 继续保持 SQLite/PostgreSQL 兼容。
- 为后续迁入 Alembic 降低成本。

验收：
- 新库初始化和旧库升级均通过测试。
- `/api/system/migrations` 能展示所有迁移状态。

## 需要外部条件

### T4 NAS 启用容器内置 FFmpeg

状态：待外部确认

阻塞：
- 需要确认 NAS 构建环境可访问 apt 源，或提供内网预构建镜像。

当前可用能力：
- `Dockerfile.ffmpeg`
- `docker-compose.ffmpeg.yml`
- `/api/system/runtime`

验收：
- NAS 使用 FFmpeg 镜像启动。
- `/api/system/runtime` 返回 `ffmpeg_available=true`。
- 语音/视频转码样本验收通过。

### T5 Forgejo SSH 鉴权

状态：待外部配置

阻塞：
- 本机缺少 `C:\Users\Administrator\.ssh\id_ed25519_forgejo`。
- 需要将对应公钥加入 Forgejo 账号 SSH Key 或仓库 Deploy Key。

当前可用能力：
- HTTPS token 推送可用。
- `scripts/git_connectivity_check.py --remote origin --skip-https` 可复查 SSH 链路。

验收：
- SSH 检查通过。
- 评估是否将长期 remote 切换为 SSH。

## 后续增强

### T6 完整数据库用户与登录态

状态：待处理

目标：
- 在现有静态 Token 与数据库托管 Token 基础上，引入数据库用户、登录态和更细粒度前端角色 UI。

验收：
- 用户登录、退出、角色授权、Token 轮换均有测试。

### T7 完整 Alembic 迁移体系

状态：待处理

目标：
- 在轻量迁移稳定后引入 Alembic。
- 将未来复杂结构变更迁入版本化迁移脚本。

验收：
- 本地测试和 NAS 部署均不依赖手工改表。
