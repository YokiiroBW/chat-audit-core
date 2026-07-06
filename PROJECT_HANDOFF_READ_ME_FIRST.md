---
title: "chat-audit-core 项目续作交接说明"
project: "QQ / 微信多租户社交资产审计系统"
repo: "chat-audit-core"
forgejo: "http://192.168.31.210:18085/YokiiroBW/chat-audit-core"
branch: "main"
updated_at: "2026-07-06"
read_this_first: true
---

# READ ME FIRST：chat-audit-core 项目续作交接说明

本文用于后续对话快速接手，不需要翻完整聊天记录。不要在本文档里写入 token、密码、私钥或 NAS 敏感凭据。

## 当前状态

- 当前分支：`main`
- 远端：局域网 Forgejo
- 当前主线：QQ/NapCat 消息备份、离线可用性和审计体验完善
- 微信路线：已封存，保留已完成代码，不再作为当前开发队列推进
- 本地最近全量测试：`165 passed`
- 最新提交：以 `git log -1 --oneline` 为准
- NAS 基础验收：健康检查、首页离线资源、管理鉴权和 OneBot 路由已通过
- NAS 离线验收：2026-07-06 部署后扫描 9269 条消息、3659 个媒体资产、300 个头像，`offline_ready=true`，缺失项 0
- NAS FFmpeg：推荐使用 `docker-compose.ffmpeg.yml` 内置静态 FFmpeg 路线，`/api/system/runtime` 返回 `ffmpeg_available=true`

## 已完成能力

- QQ/NapCat OneBot 11 反向 WebSocket：`/onebot/v11/ws`
- 当前接入机器人：`napcat2`，端口 `26109`，`self_id=1449801200`
- 适配器与机器人身份档案分离：`Adapter.current_robot_id` + `BotProfile`
- 全局消息池去重与 `RobotMessage` 主视角隔离
- CQ 文本、图片、动画表情、语音、视频、文件包/文档、卡片、合并转发、回复预览和本地缓存
- 卡片网页快照、本地媒体索引、头像与群资料缓存
- 图片弹窗预览、合并消息展开、回复消息预览
- 全离线验收：`GET /api/offline/audit`
- 离线修复：`POST /api/offline/repair`
- 导出/导入 JSON，带媒体文件嵌入、checksum、系统签名
- 自动备份定时任务、数据库配置覆盖与手动触发：`/api/backup/status`、`/api/backup/settings`、`/api/backup/run`
- 仪表盘统计：`/api/dashboard`
- 审计日志：`audit_logs` 与 `GET /api/audit/logs`
- 高风险接口限流：`HIGH_RISK_RATE_LIMIT_PER_MINUTE`
- 多角色管理 Token：静态 `ADMIN_API_TOKENS` + 数据库托管 `admin_tokens`
- 数据库用户与登录态：`admin_users`、`admin_sessions`、`/api/auth/login`、`/api/auth/me`、`/api/auth/logout`
- 数据库用户密码重置、会话列表、强制下线
- 数据库迁移体系：轻量迁移注册表、`schema_migrations`、`GET /api/system/migrations`、Alembic CLI
- 运行时状态：`GET /api/system/runtime`
- 可选 FFmpeg 转码：内置静态 FFmpeg Docker 覆盖文件为推荐路线
- 离线审计缺失原因分类、原因汇总和前端缺失说明
- 媒体回填失败原因细分、结构化统计和结果展示
- 合并转发自动后台拉取与媒体缓存，断开连接时清理后台任务
- 回复消息点击跳转与原消息缺失态提示
- 聊天区空状态压缩，减少无效说明占位
- 角色抓取策略：
  - `GET /api/bots/{robot_id}/capture-targets`
  - `PUT /api/bots/{robot_id}/capture-policies/{target_type}/{target_id}`
  - `DELETE /api/bots/{robot_id}/capture-policies/{target_type}/{target_id}`
  - 空策略默认记录所有会话
  - 黑名单目标跳过入库和缓存
  - 白名单存在时只抓取白名单目标
  - 内容项分开控制：文字、图片、动画表情、语音、视频、文件包/文档
  - 文件包/文档仅指 `CQ:file`，zip、安装包、文档等；图片、动画表情、语音不归入文件范畴
  - 文件包/文档下载默认关闭

## 微信路线封存记录

已完成但封存：

- 微信 Hook 通用入口：`POST /api/wechat/events`
- 外部消息兼容入口：`POST /api/receive_external_msg`
- 媒体上传入口：`POST /api/external/media`、`POST /api/wechat/media`
- PC 微信托盘采集器骨架：`wechat_tray_adapter/`
- 微信样本回放测试

封存原因：

- 新版微信 4.x 与当前 `wcferry 39.5.2.0` 不兼容。
- 已下载并校验 `tom-snow/wechat-windows-versions` 的 `WeChatSetup-3.9.12.51.exe`，SHA256 匹配，腾讯签名有效，安装后 `WeChat.exe` 版本为 `3.9.12.51`。
- WCF 注入旧版微信时日志显示 `MapImage => 0xC000010A`。
- 本机开启 HVCI/内存完整性，注入链路受阻。
- 关闭主力机内存完整性风险较高。
- 独立采集机或虚拟机会占用同一个微信号的 PC 登录。
- PadLocal/iPad 协议依赖外部 Token，存在账号风控。
- 数据库读取无法保证未点开的图片、视频、文件完整落地。

恢复条件：

- 用户明确重新开启微信路线。
- 或出现稳定、低风险、不占用当前 PC 登录、可后台缓存媒体的方案。

## NAS 信息

- NAS：`192.168.31.210`
- 服务 URL：`http://192.168.31.210:8000/`
- Stack 路径：`/volume1/Download/dockge/stacks/chat-audit-core`
- 安全部署脚本：`.tmp/nas_deploy_safe.py`
- 基础验收脚本：`.tmp/nas_acceptance.py`
- API 调用脚本：`.tmp/nas_api_call.py`

## 常用验证命令

本地全量测试：

```powershell
New-Item -ItemType Directory -Force .tmp\pytest-all | Out-Null
$env:TEMP=(Resolve-Path .tmp\pytest-all).Path
$env:TMP=$env:TEMP
.\.venv\Scripts\python.exe -m pytest --basetemp=.tmp\pytest-all
```

NAS 安全部署：

```powershell
C:\Users\Administrator\Documents\Hermes\QQWXTB\.venv\Scripts\python.exe .tmp\nas_deploy_safe.py
```

NAS 基础验收：

```powershell
C:\Users\Administrator\Documents\Hermes\QQWXTB\.venv\Scripts\python.exe .tmp\nas_acceptance.py
```

NAS 离线验收：

```powershell
$env:API_METHOD='GET'
$env:API_PATH='/api/offline/audit?limit=50000&issue_limit=20'
C:\Users\Administrator\Documents\Hermes\QQWXTB\.venv\Scripts\python.exe .tmp\nas_api_call.py
```

Forgejo SSH 推送：

```powershell
$env:GIT_TERMINAL_PROMPT='0'
$env:GIT_SSH_COMMAND='"C:\Program Files\Git\usr\bin\ssh.exe" -i "C:\Users\Administrator\.ssh\id_ed25519_forgejo" -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -p 2222'
git push ssh://git@192.168.31.210:2222/YokiiroBW/chat-audit-core.git main
```

## 当前未完成 / 后续队列

队列文件：`DEVELOPMENT_QUEUE.md`、`TASK_QUEUE.md`、`CURRENT_QUEUE_STATUS.md`

当前 QQ 主线：

- QQ/NapCat 当前核心开发队列已进入生产样本持续验收阶段。
- 后续重点是发现真实样本缺口后补测试和修复，而不是继续扩展微信路线。
- 每次运行时或部署行为变更后继续执行本地全量测试、Forgejo 推送、NAS 部署和离线审计。

## 继续推进规则

- 每个可验收版本都要：测试、中文提交、推送 Forgejo。
- 涉及运行时或部署行为的变更，还要部署 NAS 并验收。
- 继续忽略 `.tmp/` 内本地脚本和测试产物。
- 不要回滚用户或运行环境产生的数据。
