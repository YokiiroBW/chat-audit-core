# QQ社交资产审计平台

一个基于 NapCat / OneBot 11 的 QQ 聊天记录本地备份与审计平台。

它可以把机器人账号能看到的群聊、私聊、图片、语音、视频、文件、卡片消息、回复消息和合并转发消息保存到本地，方便之后搜索、查看、导出和离线留存。

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
