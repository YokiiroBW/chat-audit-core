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
- FFmpeg 可选转码：内置静态 FFmpeg 镜像构建可用，NAS `/api/system/runtime` 返回 `ffmpeg_available=true`，WAV 转 MP3 smoke test 通过。
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

### T9 微信 PC 托盘采集适配器

状态：通用微信 Hook 入口已完成；下一阶段改为 Windows PC 静默托盘采集器，托盘软件内置集成 WeChatFerry，不要求用户单独启动 WeChatFerry。

目标：

- PC 端启动后无命令行窗口、无默认主窗口，仅显示系统托盘图标。
- 托盘程序内置调用 `wcferry`/WeChatFerry 监听官方 PC 微信消息流。
- 自动下载图片、语音、视频、文件等本地媒体，并上传到 NAS。
- 支持断线队列、自动重连、失败重试和本地日志。
- NAS 端补充外部消息接收与 Multipart 文件上传接口，复用现有微信归一化、媒体缓存、离线审计和查询展示链路。
- 增加 WeChatFerry 真实样本回放、安装说明和托盘程序配置说明。

验收：

- 托盘程序可用 `pythonw.exe` 或打包为 `--noconsole` exe 静默启动。
- 用户不需要手动启动 WeChatFerry 服务。
- 新微信消息可从 PC 端自动同步到 NAS 并在 Web UI 查询。
- 图片、语音、视频、文件可离线打开。
- 不影响已有 QQ 和通用微信 Hook 流程。
- 全量测试通过，并推送 Forgejo；涉及 NAS 接口变更时完成 NAS 部署验收。
