# QQ社交资产审计平台

一个基于 NapCat / OneBot 11 的 QQ 聊天记录本地备份与审计平台。

它可以把机器人账号能看到的群聊、私聊、图片、语音、视频、文件、卡片消息、回复消息和合并转发消息保存到本地，方便之后搜索、查看、导出和离线留存。

## 项目说明

天枢只读证据桥接的本地候选接口见 [TS-061 说明](docs/evidence-read-candidate.md)。默认关闭，必须配置独立服务凭据和精确会话范围；该候选不代表跨产品 I06 已发布。

本项目源于本人对 QQ 聊天资产本地备份与离线留存的个人需要，由本人提出需求，并安排天才程序员协作开发。开发过程中也使用 Claude、GPT 等 AI 编程助手辅助方案讨论、代码审阅和实现推进。

## 致谢

感谢 [NapCatQQ](https://github.com/NapNeko/NapCatQQ) 团队提供的 NapCat / OneBot 11 协议端框架支持。本项目的 QQ 消息接入能力建立在 NapCat 反向 WebSocket 能力之上。

## 适合做什么

- 长期备份 QQ 群聊和私聊记录
- 保存图片、动画表情、语音、视频、文件等聊天资产
- 查看合并转发、回复消息、卡片链接等特殊消息
- 多个机器人账号分开管理，避免消息串线
- 按群聊或个人设置黑名单、白名单和抓取范围
- 在断网后继续查看已经缓存过的历史记录
- 定期自动备份，必要时导出和恢复

请只备份你自己有权限管理和保存的聊天数据。

## 主要功能

- 支持 NapCat / OneBot 11 反向连接
- 支持多个适配器和多个 QQ 账号
- 自动识别机器人身份，适配器换号后不会混淆旧账号数据
- 支持群聊名称、头像、私聊头像本地缓存
- 支持文本、链接、图片、动画表情、语音、视频、普通文件
- 支持回复消息跳转、戳一戳、合并转发消息
- 支持图片弹窗预览、滚轮缩放、拖拽查看细节
- 支持 B 站、小程序等卡片解析真实网页链接
- 支持黑名单 / 白名单抓取策略
- 支持自动备份、导入、导出和离线资产检查
- 默认使用 SQLite，安装简单；也可以切换到 PostgreSQL

## 快速开始

先复制配置文件：

```bash
cp .env.example .env
```

编辑 `.env`，至少修改下面几项：

```text
APP_SECRET_KEY=换成一段长随机字符串
ADMIN_API_TOKEN=换成你的管理后台密码
ONEBOT_ACCESS_TOKEN=换成你的 NapCat 连接密码
SYSTEM_INSTANCE_ID=换成你的实例名称
```

启动服务：

```bash
docker compose up -d --build
```

打开页面：

```text
http://服务器IP:8000/
```

健康检查：

```text
http://服务器IP:8000/health
```

## 连接 NapCat

在 NapCat 中配置反向 WebSocket：

```text
ws://服务器IP:8000/onebot/v11/ws?adapter_id=napcat1&access_token=你的ONEBOT_ACCESS_TOKEN
```

如果你有多个 NapCat，可以使用不同的 `adapter_id`：

```text
napcat1
napcat2
napcat3
```

每个连接成功的 QQ 账号都会自动建立自己的身份档案。

## 数据保存在哪里

默认数据目录：

```text
data/chat_audit.sqlite3  # 默认数据库
data/storage             # 图片、语音、视频、头像、卡片等缓存
data/backups             # 自动备份文件
```

这些目录不会包含在发布源码包里。请定期备份 `data/` 目录。

## 使用 PostgreSQL

默认 SQLite 已经可以直接使用。如果你希望使用 PostgreSQL，先在 `.env` 里填写：

```text
POSTGRES_DB=chat_audit
POSTGRES_USER=chat_audit
POSTGRES_PASSWORD=换成数据库密码
DATABASE_URL=postgresql+asyncpg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@postgres:5432/${POSTGRES_DB}
```

然后用 PostgreSQL 配置启动：

```bash
docker compose -f docker-compose.yml -f docker-compose.postgres.yml up -d --build
```

## 可选 FFmpeg

如果需要更好的语音、视频播放兼容性，可以使用带 FFmpeg 的构建：

```bash
docker compose -f docker-compose.yml -f docker-compose.ffmpeg.yml up -d --build
```

也可以挂载宿主机已有的 FFmpeg，配置见 `docker-compose.ffmpeg-host.yml`。

## 备份和恢复

系统默认按 UTC 每天 03:00 自动备份（北京时间 11:00）。自动备份和网页里的“立即备份”都会把任务交给独立的 `backup-worker`：数据库按游标输出为有界 JSONL 分块，媒体作为 `.cacb` 归档中的独立成员流式写入，不会再把全部媒体转成 Base64 放进一个巨型 JSON。

`backup-worker` 默认有 768 MiB 的容器硬上限、相同的 memory+swap 上限（即不使用容器 swap）和 640 MiB 进程地址空间上限。完整文件只有在写入、刷盘并重新校验 checksum/HMAC 后才原子发布；中断或磁盘满只会留下隐藏的失败证据，不会伪装成可恢复备份。

查看 worker 和任务状态：

```bash
docker compose ps backup-worker
docker compose exec backup-worker python -m app.backup.worker healthcheck
docker compose exec app python -m app.backup.cli status <job-id>
```

旧 v1-v3 JSON/JSON.GZ 的小包仍可从网页导入。大型旧包应使用有界转换器生成 v4 `.cacb`，新旧文件并存，转换不会覆盖原文件：

```bash
docker compose run --rm --no-deps backup-worker \
  python -m app.backup.cli convert-legacy /app/data/backups/<legacy-file>.json
```

完整校验和隔离恢复命令见 [DISASTER_RECOVERY.md](DISASTER_RECOVERY.md)。网页的旧 JSON 导出/导入只保留为有硬大小上限的兼容功能；生产全量备份必须使用 worker 路径。

建议同时备份：

```text
data/
.env
```

`.env` 里包含密钥和连接密码，不要公开上传。

## 安全提醒

- 不要把 `.env`、数据库、聊天媒体缓存提交到公开仓库
- 不要公开你的 `ADMIN_API_TOKEN` 和 `ONEBOT_ACCESS_TOKEN`
- 对外网开放前请先配置强密码和反向代理访问控制
- 请遵守聊天平台规则和当地法律法规

## Windows QQNT Collector

普通 Windows 用户可以直接运行托盘界面：

```powershell
.\dist\chat-audit-qq-collector-gui.exe
```

首次启动在“设置”页填写 QQ 账号、QQNT 数据目录、服务器地址和 API Token。
关闭窗口后程序继续在系统托盘运行。界面提供数据库兼容检测、同步控制、
模拟上传测试、队列状态、失败重试和脱敏诊断包。

命令行工具继续用于自动化和高级诊断。

仓库中的 `collector/` 已提供 Windows Collector 的基础框架，包括：

- TOML 配置与敏感字段拦截；
- Windows DPAPI 凭据存储；
- 本地 SQLite 状态库、上传队列、退避重试和 dead-letter；
- 媒体 staging 与两阶段上传；
- 调度器、CLI 状态页和模拟消息导入。

快速验证：

```powershell
Copy-Item collector\collector.example.toml collector.toml
python -m collector --config collector.toml credential set api_token
python -m collector --config collector.toml simulate
python -m collector --config collector.toml status
python -m collector --config collector.toml run-once
python -m collector --config collector.toml discover
```

Collector 已提供数据目录发现、WAL 一致性快照、只读 SQLite 连接、复合
增量游标、消息和媒体解析、持久上传队列及 Windows 打包。读取始终使用
只读事务，不会修改或修复 QQNT 数据库。真实 `SQLite header 3 / QQ_NT DB`
数据库在缺少兼容 NT VFS 时会明确显示不兼容，不会伪报同步成功。

## 当前状态

`v1.0.0` 是第一个稳定版本，主线功能已经围绕 QQ / NapCat 聊天记录备份、浏览、搜索、导出和离线留存闭环。


### QQNT 数据库密钥

Collector 读取 QQNT 本地数据库需要 16 字节 ASCII 密钥，在 GUI 的「设置」页
以掩码字段填写。密钥与 API Token 一并保存在当前 Windows 用户的 DPAPI 作用域，
绝不会写入 `collector.toml`。读取时先生成一致性快照，再把剥离 1024 字节
自定义头的临时副本交给 SQLCipher，并以 `query_only` 和拒写 authorizer 打开，
不会修改 QQ 的原始文件。CLI 等价命令：

```powershell
python -m collector --config collector.toml credential set qqnt_database_key
python -m collector --config collector.toml probe C:\path\to\nt_msg.db --snapshot
python -m collector --config collector.toml scan C:\path\to\nt_msg.db --mode initial
```

填入 16 字节密钥只说明格式正确，不代表密钥可用。托盘此时提示「能否读取将在
首次同步时实测确认」，可读性由首次同步真实打开剥头副本来证明，失败会作为扫描
问题上报，托盘不会仅凭密钥长度声称可读。停止码与含义：缺少 SQLCipher 运行时
返回 `DB_SQLCIPHER_UNAVAILABLE`；缺少密钥或长度不是 16 字节返回
`DB_KEY_INVALID`；自定义头之后不是 SQLCipher 密文体则返回 `DB_VFS_UNSUPPORTED`。
