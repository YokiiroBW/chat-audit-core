---
title: "chat-audit-core 项目续作交接说明"
project: "QQ / 微信多租户社交资产审计系统"
repo: "chat-audit-core"
forgejo: "http://192.168.31.210:18085/YokiiroBW/chat-audit-core"
branch: "main"
updated_at: "2026-07-05"
read_this_first: true
---

# READ ME FIRST：chat-audit-core 项目续作交接说明

本文用于后续对话快速接手，不需要翻完整聊天记录。不要在本文档里写入 token、密码、私钥或 NAS 敏感凭据。

## 当前状态

- 当前分支：`main`
- 远端：`origin/main`
- 同步状态：本轮完成后需推送 Forgejo，查看 `git status --short --branch` 确认
- 最新提交：本轮 FFmpeg 静态镜像修复提交（以 `git log -1 --oneline` 为准）
- 本地全量测试：`144 passed`
- 最近一次 NAS 部署验收：FFmpeg 静态镜像方案已在 NAS 启动并完成 smoke test
- NAS 基础验收：健康检查 200、首页 200、管理鉴权 401/200 正常
- NAS 迁移状态：`20260705_008_capture_target_policies` 已应用
- NAS 抓取策略接口：`GET /api/bots/{robot_id}/capture-targets` 正常返回已发现群聊/私聊、名称、头像和策略
- NAS FFmpeg 状态：`docker-compose.ffmpeg.yml` 已改为离线安装 `vendor/wheels/imageio_ffmpeg-0.6.0-py3-none-manylinux2014_x86_64.whl` 内置静态 FFmpeg，NAS 容器内 `/api/system/runtime` 返回 `ffmpeg_available=true`，版本 `7.0.2-static`；WAV 转 MP3 smoke test 已通过。宿主机挂载方案仍保留为兼容路径，但 NAS 宿主机动态库组合曾出现实际转码段错误，不作为推荐路径。

## 已完成能力

- QQ/NapCat OneBot 11 反向 WebSocket：`/onebot/v11/ws`
- 当前接入机器人：`napcat2`，端口 `26109`，`self_id=1449801200`
- 适配器与机器人身份档案分离：`Adapter.current_robot_id` + `BotProfile`
- 全局消息池去重与 `RobotMessage` 主视角隔离
- CQ 图片、动画表情、语音、视频、文件包/文档、卡片、合并转发、回复预览和本地缓存
- 卡片网页快照、本地媒体索引、头像与群资料缓存
- 图片弹窗预览、合并消息展开、回复消息预览
- 全离线验收：`GET /api/offline/audit`
- 离线修复：`POST /api/offline/repair`
- 导出/导入 JSON，带媒体文件嵌入、checksum、系统签名
- 自动备份定时任务、数据库配置覆盖与手动触发：`/api/backup/status`、`/api/backup/settings`、`/api/backup/run`
- 仪表盘统计：`/api/dashboard`
- 微信 Hook 通用入口：`POST /api/wechat/events`，带样本回放测试
- 操作审计：`audit_logs` 与 `GET /api/audit/logs`
- 高风险接口限流：`HIGH_RISK_RATE_LIMIT_PER_MINUTE`
- 多角色管理 Token：静态 `ADMIN_API_TOKENS` + 数据库托管 `admin_tokens`
- 数据库用户与登录态：`admin_users`、`admin_sessions`、`/api/auth/login`、`/api/auth/me`、`/api/auth/logout`
- 数据库用户密码重置、会话列表、强制下线
- 数据库迁移体系：轻量迁移注册表、`schema_migrations`、`GET /api/system/migrations`、Alembic CLI
- 运行时状态：`GET /api/system/runtime`
- 可选 FFmpeg 转码：推荐使用内置静态 FFmpeg 覆盖 `Dockerfile.ffmpeg` + `docker-compose.ffmpeg.yml`；宿主机挂载覆盖 `docker-compose.ffmpeg-host.yml` 仅作兼容路径
- 角色抓取策略：
  - `GET /api/bots/{robot_id}/capture-targets`
  - `PUT /api/bots/{robot_id}/capture-policies/{target_type}/{target_id}`
  - `DELETE /api/bots/{robot_id}/capture-policies/{target_type}/{target_id}`
  - 空策略默认记录所有会话
  - 黑名单目标跳过入库和缓存
  - 白名单存在时仅抓取白名单目标
  - 内容项分开控制：文字、图片/动画表情、语音、视频、文件包/文档
  - 文件包/文档仅指 `CQ:file`，zip、安装包、文档等；图片、动画表情、语音不归入文件范围
  - 文件包/文档下载默认关闭

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

队列文件：`DEVELOPMENT_QUEUE.md`、`TASK_QUEUE.md`

仍需处理：

- 微信 Hook 专用映射：当前已支持常见字段、数字类型和通用样本回放；后续应根据最终选定客户端追加专属真实样本和部署说明。
- 持续更新交接文档：每次版本推进后更新最新提交、测试数量、NAS 状态和剩余队列。

## 继续推进规则

- 每个可验收版本都要：测试、中文提交、推送 Forgejo。
- 涉及运行时或部署行为的变更，还要部署 NAS 并验收。
- 继续忽略 `.tmp/` 内本地脚本和测试产物。
- 不要回滚用户或运行环境产生的数据。
