# 灾难恢复与备份演练指南

## 备份策略

- 自动备份默认由 `AUTO_BACKUP_CRON=0 3 * * *` 控制，每天 03:00 执行一次。
- 保留策略默认由 `AUTO_BACKUP_KEEP_LATEST=7` 控制，仅保留最近 7 份自动备份。
- 备份目录由 `BACKUP_ROOT` 控制，Docker 部署默认挂载到 `./data/backups:/app/data/backups`。
- 媒体文件目录由 `STORAGE_ROOT` 控制，Docker 部署默认挂载到 `./data/storage:/app/data/storage`。
- 导出包包含消息、机器人视角、适配器、资料缓存、抓取策略、媒体索引和可嵌入的本地媒体文件。
- manifest 包含 checksum 和 HMAC signature；恢复前必须先做校验。

## 恢复目标

- RTO：普通单机/NAS 部署目标为 1 小时内恢复服务。
- RPO：使用默认每日自动备份时，最多丢失 24 小时数据；如业务要求更低 RPO，应提高 `AUTO_BACKUP_CRON` 频率。

## 完整恢复步骤

1. 准备新环境。

```bash
git clone <forgejo-repo-url> chat-audit-core
cd chat-audit-core
cp .env.example .env
```

2. 写入生产配置。

必须确认以下配置与旧环境一致或已按新环境调整：

- `APP_SECRET_KEY`
- `SYSTEM_INSTANCE_ID`
- `POSTGRES_DB`
- `POSTGRES_USER`
- `POSTGRES_PASSWORD`
- `ONEBOT_ACCESS_TOKEN`
- `ADMIN_API_TOKEN` 或 `ADMIN_API_TOKENS`
- `STORAGE_ROOT`
- `BACKUP_ROOT`

3. 恢复持久化目录。

将旧环境的 `data/storage/` 和 `data/backups/` 复制到新环境。若只有导出包没有完整目录，也可以先启动服务，再通过导入接口恢复可嵌入的媒体文件。

4. 启动数据库与应用。

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f app
```

5. 执行迁移。

容器启动会自动创建表并执行轻量迁移。需要手动确认 Alembic 状态时执行：

```bash
docker compose exec app python -m alembic upgrade head
docker compose exec app python -m alembic current
```

6. 校验备份包。

```bash
curl -X POST http://127.0.0.1:8000/api/import/validate \
  -H "Authorization: Bearer $ADMIN_API_TOKEN" \
  -H "Content-Type: application/json" \
  --data-binary @data/backups/<backup-file>.json
```

如果是 `.json.gz` 包，先解压或通过前端导入页面上传解析后的内容。

7. 导入备份包。

```bash
curl -X POST http://127.0.0.1:8000/api/import \
  -H "Authorization: Bearer $ADMIN_API_TOKEN" \
  -H "Content-Type: application/json" \
  --data-binary @data/backups/<backup-file>.json
```

8. 做离线资产验收。

```bash
curl "http://127.0.0.1:8000/api/offline/audit?limit=50000" \
  -H "Authorization: Bearer $ADMIN_API_TOKEN"
```

如报告显示缺失项，可先确认源机器人在线，再执行：

```bash
curl -X POST "http://127.0.0.1:8000/api/offline/repair?limit=50000" \
  -H "Authorization: Bearer $ADMIN_API_TOKEN"
```

9. 验收服务。

- `/health` 返回 `status=ok`。
- `/metrics` 可访问，并能看到 HTTP、媒体下载、WebSocket 和限流指标。
- Web 控制台能看到机器人、群名称、头像、历史消息、图片、语音、视频、文件、卡片和合并转发缓存。
- 随机抽查至少 3 个群聊和 3 个私聊。
- 断网后刷新已加载前端，页面壳资源仍可打开；历史消息依赖本地数据库和本地媒体缓存。

## 演练流程

建议每月至少执行一次恢复演练：

1. 在测试目录或测试 NAS 上拉起一套隔离环境。
2. 从生产 `BACKUP_ROOT` 复制最新一份自动备份和对应 `STORAGE_ROOT`。
3. 按“完整恢复步骤”导入并启动。
4. 执行 `/api/import/validate`、`/api/offline/audit`、`/health`、`/metrics`。
5. 记录恢复耗时、失败项、缺失媒体数量和人工处理步骤。
6. 演练后销毁测试环境，避免测试机器人或旧 token 长期暴露。

## 失败处理

- 导入校验失败：优先检查 checksum/signature、备份文件是否被截断、`APP_SECRET_KEY` 是否与原环境一致。
- 数据库不可用：检查 `docker compose ps`、PostgreSQL healthcheck、`DATABASE_URL` 和卷挂载。
- 媒体文件缺失：确认 `data/storage/` 是否完整复制；再运行离线修复。
- 头像或群名缺失：确认机器人在线后打开相关会话，或运行离线修复补缓存。
- 自动备份失败：查看 `BACKUP_ROOT/failures.log` 和应用日志。

## 演练记录模板

```text
演练日期：
演练人员：
备份文件：
恢复环境：
RTO 实测：
RPO 实测：
/health 结果：
/metrics 结果：
离线审计结果：
缺失项与处理：
结论：
```
