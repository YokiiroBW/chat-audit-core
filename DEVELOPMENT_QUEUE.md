# 开发队列

本队列用于承接当前审计出的未完成项目。后续每完成一个可验收版本，按以下流程推进：

1. 更新代码与测试。
2. 运行相关测试和全量测试。
3. 使用中文提交信息提交。
4. 推送到局域网 Forgejo 仓库。
5. 对需要部署的功能，部署到 NAS 并执行接入验收。

## P0 当前无阻塞项

当前主线可用，全量测试通过。以下项目均为后续能力增强和生产化收口。

## P1 优先开发

### 1. FFmpeg 与媒体转码流水线

状态：已完成第三版

目标：
- Docker 运行时包含可用 FFmpeg。
- 对语音、视频提供可选转码能力，提高浏览器播放兼容性。
- 转码失败不影响原始媒体落盘，保留可审计记录。

已完成：
- 应用侧可选转码开关。
- 语音、视频转码成功路径与失败回退。
- FFmpeg 不可用时自动保存原始文件。
- 默认 Docker 镜像继续保持离线友好，不依赖 apt 源。
- 新增 `Dockerfile.ffmpeg` 与 `docker-compose.ffmpeg.yml`，用于允许联网构建时显式生成内置 FFmpeg 镜像。
- 新增 `docker-compose.ffmpeg-host.yml`，用于宿主机/NAS 已有 FFmpeg 时直接挂载可执行文件。
- 新增 `GET /api/system/runtime`，可查看 `ffmpeg_available`、`ffmpeg_version` 与转码配置。

剩余：
- NAS 默认部署仍使用离线友好镜像；如要启用 FFmpeg，可选择宿主机挂载路径，或在 NAS 构建环境可访问 apt 源时使用自动安装镜像。

验收：
- 单元测试覆盖转码成功、FFmpeg 不可用、转码失败回退。
- Dockerfile 与配置文档一致，且 NAS 构建不依赖 apt 源。
- 可选 FFmpeg Dockerfile 与 compose 覆盖文件有部署文件测试。
- 全量测试通过。

### 2. 真实微信适配器

状态：已完成第三版

目标：
- 增加微信接入适配器入口。
- 将微信消息转换为统一内部消息模型。
- 复用现有本地媒体缓存、主视角隔离、导出导入与离线验收能力。

已完成：
- 新增 `POST /api/wechat/events` 通用 Hook 接收入口。
- 支持常见微信 Hook 字段名自动归一化。
- 文本、图片、语音、视频、文件和卡片消息可转换为现有内部消息/CQ 表达。
- 入库后使用 `platform=wechat`，并复用媒体缓存、查询和资料缓存。
- 支持顶层、`data`、`payload`、`msg`、`message` 嵌套字段。
- 支持常见微信字段大小写差异和数字 `MsgType`：文本、图片、语音、视频、表情、分享。
- 支持群聊文本中的 `sender_wxid:\n内容` 前缀拆分。
- 新增 `tests/fixtures/wechat_hook_samples.json`，用样本回放覆盖文本、图片、语音、表情、分享卡片和群聊发送者前缀。

剩余：
- 等最终选定微信 Hook 客户端后，可继续追加真实客户端专属样本和部署说明。

验收：
- 模拟微信文本、图片、文件消息可入库并查询。
- `platform=wechat` 不被 QQ 专属逻辑覆盖。
- 全量测试通过。

### 3. 导出包系统识别码签名

状态：已完成

目标：
- 在导出 manifest 中加入系统实例标识与签名字段。
- 导入时展示来源系统、签名校验状态和风险提示。
- 保持已有 checksum 校验兼容。

已完成：
- 导出包写入 `source.system`、`source.instance_id` 与 `signature`。
- 签名使用 HMAC-SHA256，签名密钥来自 `APP_SECRET_KEY`。
- 导入校验报告展示来源和签名状态。
- 旧版无签名包保持兼容，签名错误会阻止导入。

验收：
- 签名正常、签名缺失、签名不匹配均有测试。
- 旧包仍可校验导入，但会标记签名缺失。

## P2 生产化增强

### 4. 自动备份前端配置入口

状态：已完成

目标：
- 设置页展示自动备份状态、cron、保留数量。
- 支持手动触发一次备份。
- 后端提供只读/写入配置接口，避免误写敏感环境变量。

已完成：
- 新增 `GET /api/backup/status`。
- 新增 `PATCH /api/backup/settings`，将 cron/retention 覆盖项写入数据库并记录审计日志。
- 新增 `POST /api/backup/run` 手动备份接口。
- 设置页展示自动备份状态、cron、保留数量、配置来源、备份数量和最新备份文件。
- 设置页支持保存 cron/retention，并可恢复 `.env` 默认值。
- 设置页支持立即备份，并刷新仪表盘。

验收：
- API 和前端测试覆盖。
- 手动备份生成文件并进入仪表盘统计。
- 配置变更写入 `audit_logs`。

### 5. 生产权限、限流与操作审计

状态：已完成第四版

目标：
- 对管理接口增加操作审计日志。
- 为高风险接口增加简单限流或保护策略。
- 为后续多角色权限预留模型。

已完成：
- 新增 `audit_logs` 表。
- 新增 `GET /api/audit/logs` 查询接口。
- 鉴权失败、删除适配器、媒体回填、离线修复、导入 JSON、手动备份会写入审计记录。
- 高风险写操作增加每分钟简单限流，配置项为 `HIGH_RISK_RATE_LIMIT_PER_MINUTE`。
- 新增 `ADMIN_API_TOKENS` 多角色静态 Token 配置。
- 支持 `viewer`、`operator`、`admin` 三类权限边界，旧 `ADMIN_API_TOKEN` 继续保持最高权限。
- 新增 `admin_tokens` 表和数据库托管 Token API。
- 支持创建、列表、吊销、轮换数据库 Token；完整 token 只在创建/轮换时返回一次，数据库仅保存哈希和短前缀。
- 新增 `admin_users` 和 `admin_sessions`，支持数据库用户、密码登录、当前身份查询、退出和用户禁用。
- 鉴权链路同时支持静态 Token、数据库托管 Token 和数据库用户会话。
- Web 控制台账号设置面板支持数据库托管 Token 的列表/创建/吊销，以及数据库用户创建/列表/禁用、密码重置、会话列表、强制下线、登录/退出、当前角色显示。
- 新增数据库用户密码重置接口，重置后会吊销该用户全部活跃会话。
- 新增数据库用户会话列表和按 session 强制下线接口。
- Web 控制台按当前角色禁用高风险控件，避免 viewer/operator 误触 admin 操作。

剩余：
- 暂无本项剩余开发；后续可按实际运维需求继续增加更细粒度角色策略。

验收：
- 导入、删除适配器、离线修复等操作写入审计记录。
- 测试覆盖鉴权失败、审计记录写入。

### 6. 数据库迁移体系

状态：已完成第四版

目标：
- 引入 Alembic 或等价迁移流程。
- 将当前自动补列逻辑固化为可追踪迁移。
- 保持 SQLite 测试与 PostgreSQL 部署兼容。

已完成：
- 新增 `schema_migrations` 表。
- 启动期兼容迁移会记录版本。
- 将启动期兼容迁移整理为 `LIGHTWEIGHT_MIGRATION_REGISTRY`，每条迁移具备版本、说明和执行函数。
- 当前已追踪 `adapters.current_robot_id`、`messages.external_message_id`、`audit_logs`、`schema_migrations`、`admin_tokens`、`system_settings`。
- 新增 `GET /api/system/migrations` 查询当前已知轻量迁移的应用状态。
- 新增旧 SQLite schema 升级回归测试。
- 新增 `migrations/versions/` Alembic 风格版本脚本骨架，并用测试校验与轻量迁移注册表一致。
- 新增 `alembic.ini` 与 `migrations/env.py`，可通过 `DATABASE_URL` 执行 `python -m alembic upgrade head`。
- 版本脚本已改为可执行幂等迁移，空库可初始化当前 schema，旧库可补齐已知兼容列。
- 新增 Alembic CLI 回归测试，覆盖空库初始化和旧库升级。

剩余：
- 暂无本项剩余开发；新依赖安装、容器内 `python -m alembic upgrade head` 和 NAS 部署均已验收。

验收：
- 新库可初始化。
- 旧库可迁移。
- CI/本地测试不依赖手工改表。

## P3 运维和文档收口

### 7. Forgejo SSH 鉴权

状态：已完成

目标：
- 配置 Forgejo SSH Key 或 Deploy Key。
- 跑通 `scripts/git_connectivity_check.py --skip-https`。
- 评估是否将长期远端切换为 SSH。

已完成：
- 已生成 Forgejo 专用 Ed25519 key：`C:\Users\Administrator\.ssh\id_ed25519_forgejo`。
- 已将公钥注册到 Forgejo 账号 SSH Key。
- 已修复 Windows 下连通性检查脚本的 SSH 私钥路径处理，并自动兼容 Git for Windows 自带 `ssh.exe`。
- `scripts/git_connectivity_check.py --remote origin --skip-https` 已通过。

验收：
- HTTPS token 和 SSH 两条链路均可检查。

### 8. 交接文档更新

状态：已完成第一版

目标：
- 更新 `PROJECT_HANDOFF_READ_ME_FIRST.md` 的最新提交、测试数量、NAS 状态和剩余队列。
- 清理旧状态描述，避免后续接手误判。

已完成：
- 交接文档已重写为当前主线状态。
- 记录 NAS 验收、剩余队列和继续推进规则。

验收：
- 文档与当前主线一致。
- 不写入 token、密码或 NAS 敏感凭据。
