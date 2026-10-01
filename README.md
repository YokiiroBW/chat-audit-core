# Source publication 2026.10.01-rc.1

This version branch publishes the integrated Tianshu source without the vendored FFmpeg wheel. The existing remote main and its history are preserved. The normal source and default deployment files remain available; the optional Dockerfile.ffmpeg expects the excluded wheel and is outside this source publication's validated build scope. Do not use this snapshot to distribute a bundled FFmpeg binary or image until its exact corresponding source and license obligations are verified. Provenance and exclusions are recorded in PUBLICATION-PROVENANCE.json.

# 💾 QQ 社交资产审计平台

> **项目简介**：一个基于 NapCat / OneBot 11 的 QQ 聊天记录本地化备份与可视化审计方案。

我们在 QQ 中沉淀的群聊、私聊、图片、语音及重要文件等数据，时常面临着因平台清理缓存、误删或账号变动而丢失的风险。

本项目旨在将机器人账号所能触达的全部聊天维度进行本地化落地，提供一个安全、可检索、可导出的离线留存平台。

天枢只读证据桥接的本地候选接口见 [TS-061 说明](docs/evidence-read-candidate.md)。默认关闭，必须配置独立服务凭据和精确会话范围；该候选不代表跨产品 I06 已发布。

## 💡 项目背景

本项目源于个人对 QQ 聊天资产本地备份与离线留存的实际需求。在明确具体业务需求后，交由天才程序员Claude与GPT等AI工具进行落地实现。

### 🤝 致谢
诚挚感谢 [NapCatQQ](https://github.com/NapNeko/NapCatQQ) 团队提供的 NapCat / OneBot 11 协议端框架支持。

---

## ✨ 核心特性

为了摆脱传统的工具机械感，平台在功能完整性与用户体验上做了深度优化：

* **全媒体资产留存**：完美适配文本、链接、大表情，并能完整缓存图片、语音、视频及普通文件。同时，对“回复消息跳转”、“戳一戳”以及复杂的“合并转发”等特殊消息进行了深度还原解析。
* **多账号隔离管理**：支持同时接入多个机器人账号（适配器）。系统具备自动识别机器人身份的能力，即使适配器换号，历史数据也绝不会产生混淆。
* **优化的浏览体验**：网页端内置了高交互性的图片查看器（支持弹窗预览、滚轮缩放、鼠标拖拽细节）。此外，系统能对 B 站、小程序等卡片消息进行主动解析，提取并展示真实的网页链接。
* **灵活的过滤策略**：提供针对群聊或个人的黑白名单机制，用户可根据实际需要精细化控制数据的抓取范围。
* **本地化低耦合**：默认采用轻量化的 SQLite 数据库，实现开箱即用。一旦数据缓存完成，即使在完全断网的离线状态下，也不影响历史记录的检索与查看。
* **备份与导出**：支持定时备份，副本还原。并且支持全量，选择性导出到文件，也支持json文件导入。

> ⚠️ **合规提示**：请确保仅对自身拥有管理或保存权限的聊天数据进行备份，严格保护他人隐私。

---

## 🚀 快速开始

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
