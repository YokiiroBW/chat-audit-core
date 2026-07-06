# 💾 QQ 社交资产审计平台

> **项目简介**：一个基于 NapCat / OneBot 11 的 QQ 聊天记录本地化备份与可视化审计方案。

我们在 QQ 中沉淀的群聊、私聊、图片、语音及重要文件等数据，时常面临着因平台清理缓存、误删或账号变动而丢失的风险。

本项目旨在将机器人账号所能触达的全部聊天维度进行本地化落地，提供一个安全、可检索、可导出的离线留存平台。

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

系统默认每天自动备份一次。你也可以在网页设置里修改备份时间、手动备份、导出数据或导入旧备份。

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

## 当前状态

`v1.0.0` 是第一个稳定版本，主线功能已经围绕 QQ / NapCat 聊天记录备份、浏览、搜索、导出和离线留存闭环。
