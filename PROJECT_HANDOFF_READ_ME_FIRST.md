---
title: "chat-audit-core 项目续作交接说明"
project: "QQ 与 微信多租户社交资产审计系统"
repo: "chat-audit-core"
forgejo: "http://192.168.31.210:18085/YokiiroBW/chat-audit-core"
branch: "main"
updated_at: "2026-07-05"
read_this_first: true
---

# READ ME FIRST：chat-audit-core 项目续作交接说明

本文件用于后续对话快速接手，不需要翻完整聊天记录。

## 当前状态

- 当前分支：`main`
- 远端：`origin/main`
- 同步状态：`behind=0 ahead=0`
- 最新提交主题：`功能：增加数据库用户登录态`
- 本地全量测试：`123 passed`
- NAS 部署：已部署最新版本
- NAS 离线验收：`offline_ready=true`

最近主线提交：

```text
本文件所在提交 测试：固化微信 Hook 样本回放
6c3e259 功能：增加管理令牌前端入口
e5a59d6 功能：增加数据库托管管理令牌
6c79790 功能：增强微信映射和运行时状态
4a94b61 功能：增加角色权限和迁移状态接口
6214dfd 功能：增加操作审计和迁移记录
30cf771 功能：增加自动备份控制入口
cf99eae 功能：增加导出包系统签名
2daff84 修复：微信消息生成本地头像
f137532 功能：增加微信 Hook 接入入口
1d4a31c 修复：避免部署期安装 FFmpeg 卡住
07a74df 功能：增加可选媒体转码
97b8496 文档：建立未完成开发队列
c035c19 功能：增加资产统计仪表盘
```

## 已完成能力

- QQ/NapCat OneBot 11 反向 WebSocket：`/onebot/v11/ws`
- 正确机器人：`napcat2`，端口 `26109`，`self_id=1449801200`
- 适配器与机器人身份档案分离：`Adapter.current_robot_id` + `BotProfile`
- 全局消息池去重与 `RobotMessage` 主视角隔离
- CQ 图片、语音、视频、文件、卡片、合并转发本地缓存
- 卡片网页快照、本地媒体索引、头像/群资料缓存
- 图片弹窗预览、合并消息展开、回复消息预览
- 全离线验收：`GET /api/offline/audit`
- 离线修复：`POST /api/offline/repair`
- 导出/导入 JSON，带媒体文件嵌入、checksum、系统签名
- 自动备份定时任务、数据库配置覆盖与手动触发：`/api/backup/status`、`/api/backup/settings`、`/api/backup/run`
- 仪表盘统计：`/api/dashboard`
- 微信 Hook 第三版入口：`POST /api/wechat/events`，支持常见嵌套字段、微信数字 `MsgType` 和群聊发送者前缀，并有 `tests/fixtures/wechat_hook_samples.json` 样本回放
- 操作审计：`audit_logs` 与 `GET /api/audit/logs`
- 高风险接口简单限流：`HIGH_RISK_RATE_LIMIT_PER_MINUTE`
- 多角色管理 Token：静态 `ADMIN_API_TOKENS` + 数据库托管 `admin_tokens`，支持 `viewer`、`operator`、`admin`，Web 设置页可创建/列表/吊销/轮换托管 Token
- 数据库用户与登录态：`admin_users`、`admin_sessions`、`/api/auth/login`、`/api/auth/me`、`/api/auth/logout`，Web 设置页可登录/退出并显示当前角色
- 轻量迁移注册表与记录：`LIGHTWEIGHT_MIGRATION_REGISTRY`、`schema_migrations` 与 `GET /api/system/migrations`
- 运行时状态：`GET /api/system/runtime`，可检查 FFmpeg 可用性和转码配置
- 可选 FFmpeg 镜像：`Dockerfile.ffmpeg` + `docker-compose.ffmpeg.yml`

## NAS 部署信息

- NAS：`192.168.31.210`
- 服务 URL：`http://192.168.31.210:8000/`
- Stack 路径：`/volume1/Download/dockge/stacks/chat-audit-core`
- 部署脚本：`.tmp/nas_deploy_current.py`
- 基础验收脚本：`.tmp/nas_acceptance.py`
- API 调用脚本：`.tmp/nas_api_call.py`

不要把 token、密码、私钥写入仓库、文档、remote URL 或 git config。推送 Forgejo 时继续使用临时 HTTP header。

## 常用验证命令

本地全量测试：

```powershell
.\.venv\Scripts\python.exe -m pytest --basetemp=.tmp\pytest-all
```

NAS 部署：

```powershell
C:\Users\Administrator\Documents\Hermes\QQWXTB\.venv\Scripts\python.exe .tmp\nas_deploy_current.py
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

临时 HTTP header 推送 Forgejo：

```powershell
$token = '从安全位置读取，不要写入文件'
$credential = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes('YokiiroBW:' + $token))
git -c "http.extraHeader=Authorization: Basic $credential" push origin main
```

## 当前未完成/后续队列

队列文件：`DEVELOPMENT_QUEUE.md`

仍需处理：

- 容器内置 FFmpeg：已有可选 FFmpeg Dockerfile/compose 覆盖文件；NAS 默认仍走离线友好镜像，启用前需确认 apt 源或使用预构建镜像。
- 微信 Hook 专用映射：当前已支持常见字段、数字类型和通用样本回放；后续应根据最终选定客户端追加专属真实样本和部署说明。
- 多角色权限增强：当前已支持静态角色 Token、数据库托管 Token 和前端管理面板；后续如需要，可继续做数据库用户、登录态和更细粒度角色 UI。
- 完整 Alembic：当前是可查询的轻量 `schema_migrations` 记录，复杂结构变更时建议迁入 Alembic。
- Forgejo SSH：当前 SSH 检查失败，原因是本机缺少 `C:\Users\Administrator\.ssh\id_ed25519_forgejo`。HTTPS token 推送可用。

## SSH 检查结果

最近执行：

```powershell
.\.venv\Scripts\python.exe scripts\git_connectivity_check.py --remote origin --skip-https
```

结果：

```text
[SSH] FAIL: ssh-key missing: C:\Users\Administrator\.ssh\id_ed25519_forgejo
[HTTPS] SKIP: --skip-https
summary_ssh=False
summary_https=True
```

修复步骤：

1. 生成或放置 Forgejo 专用 SSH Key：`C:\Users\Administrator\.ssh\id_ed25519_forgejo`
2. 将公钥加入 Forgejo 账号 SSH Key 或仓库 Deploy Key
3. 重新执行 `scripts/git_connectivity_check.py --remote origin --skip-https`

## 继续推进规则

- 每个可验收版本都要：测试、中文提交、推送 Forgejo、部署 NAS、验收。
- 继续忽略 `.tmp/` 内本地脚本和测试产物，不要提交 token 或运行数据。
- 不要回滚用户或运行环境产生的数据。
