# 灾难恢复与备份演练指南

## 备份策略

- 自动备份默认由 `AUTO_BACKUP_CRON=0 3 * * *` 控制，按 UTC 每天 03:00（北京时间 11:00）执行一次。
- 支持的 cron 子集：**分钟与小时字段**可用 `*`、数字、`a-b`、`*/步长`、`a-b/步长` 以及逗号列表；
  **日、月、星期三个字段必须是 `*`**（即只支持"每天重复"的计划）。例如 `0 */6 * * *`（每 6 小时）、
  `0 0,6,12,18 * * *`、`*/30 * * * *` 均有效；`0 3 * * 1`（每周一）不支持。
  写成不受支持的表达式时，自动备份**不会执行**，`GET /api/backup/status` 的 `cron_error`
  字段和 Web 控制台的备份状态会明确报出原因。
- 保留策略默认由 `AUTO_BACKUP_KEEP_LATEST=7` 控制，仅处理新格式中已完整发布的 `auto-backup-*.cacb`。手工备份、转换备份、隐藏失败文件和既有 v1-v3 JSON 不会被该策略删除。
- 备份目录由 `BACKUP_ROOT` 控制，Docker 部署默认挂载到 `./data/backups:/app/data/backups`。
- 媒体文件目录由 `STORAGE_ROOT` 控制，Docker 部署默认挂载到 `./data/storage:/app/data/storage`。
- 自动和手工全量备份都由独立 `backup-worker` 写成 v4 `.cacb`。数据库逻辑 section 是有界 JSONL 分块；媒体是独立归档成员，没有 Base64 巨型 JSON。
- PostgreSQL 使用单一 `REPEATABLE READ READ ONLY` 事务；SQLite 先用在线备份 API 取得一致快照，再从快照流式导出。
- manifest 的 payload checksum、payload HMAC、manifest checksum 和 manifest HMAC 都必须验证成功后才允许恢复。
- worker 默认限制为 768 MiB 且禁止容器 swap；进程地址空间再限制为 640 MiB。不要提高上限来掩盖失败，应先保留任务状态和 `failures.log` 排查。

## 恢复目标

- RTO：普通单机/NAS 部署目标为 1 小时内恢复服务。
- RPO：使用默认每日自动备份时，最多丢失 24 小时数据；如业务要求更低 RPO，应提高 `AUTO_BACKUP_CRON` 频率，
  例如 `0 */6 * * *` 把 RPO 降到 6 小时。改完在 Web 控制台的备份状态确认「下次运行」时间符合预期。

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

3. 恢复持久化目录或准备完整归档。

如果已有完整的数据库与媒体目录，可直接复制持久化目录。若使用 v4 `.cacb`，不要先写入生产目标；先按下面步骤恢复到全新的隔离数据库和媒体目录，验收后再按切换流程投产。

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

6. 校验 v4 备份包。

```bash
docker compose run --rm --no-deps backup-worker \
  python -m app.backup.cli validate /app/data/backups/<backup-file>.cacb
```

输出必须同时显示 `valid=true`、`checksum_valid=true` 和 `signature_valid=true`。命令从容器环境读取签名密钥，不会打印密钥值。

7. 先做隔离恢复。

```bash
docker compose run --rm --no-deps backup-worker \
  python -m app.backup.cli restore /app/data/backups/<backup-file>.cacb \
  --database-url sqlite+aiosqlite:////app/data/backups/.restore-drill/restore.sqlite3 \
  --storage-root /app/data/backups/.restore-drill/media \
  --work-root /app/data/backups/.restore-drill/work
```

恢复目标必须为空，且不得指向生产数据库或生产媒体目录。核对 manifest 计数、数据库表计数、消息与媒体引用关系，并对全部媒体或确定性分批媒体重算 SHA-256。任务专用恢复目录只有在证据保存完且确认无用后才可清理。

8. 兼容旧 v1-v3 备份。

网页 `/api/import/validate` 与 `/api/import` 继续支持有硬大小上限的小型 JSON/JSON.GZ。大型旧包先在受限 worker 中流式转换，原文件不会被覆盖或删除：

```bash
docker compose run --rm --no-deps backup-worker \
  python -m app.backup.cli convert-legacy /app/data/backups/<legacy-file>.json
```

转换器会重放旧格式 checksum/HMAC 契约并把媒体逐项解码为 v4 独立成员。转换结果仍须执行第 6、7 步。

9. 做离线资产验收。

```bash
curl "http://127.0.0.1:8000/api/offline/audit?limit=50000" \
  -H "Authorization: Bearer $ADMIN_API_TOKEN"
```

如报告显示缺失项，可先确认源机器人在线，再执行：

```bash
curl -X POST "http://127.0.0.1:8000/api/offline/repair?limit=50000" \
  -H "Authorization: Bearer $ADMIN_API_TOKEN"
```

10. 验收服务。

- `/health` 返回 `status=ok`。
- `/metrics` 可访问，并能看到 HTTP、媒体下载、WebSocket 和限流指标。
- Web 控制台能看到机器人、群名称、头像、历史消息、图片、语音、视频、文件、卡片和合并转发缓存。
- 随机抽查至少 3 个群聊和 3 个私聊。
- 断网后刷新已加载前端，页面壳资源仍可打开；历史消息依赖本地数据库和本地媒体缓存。

## 演练流程

建议每月至少执行一次恢复演练：

1. 在测试目录或测试 NAS 上拉起一套隔离环境。
2. 从生产 `BACKUP_ROOT` 只读取得最新一份已完成 `.cacb`，不改写原文件。
3. 按“完整恢复步骤”校验并恢复到隔离数据库/媒体目录。
4. 核对 checksum/HMAC、记录数、关键关联、媒体 SHA-256、`/health` 和 `/metrics`。
5. 记录恢复耗时、失败项、缺失媒体数量和人工处理步骤。
6. 演练后销毁测试环境，避免测试机器人或旧 token 长期暴露。

## 失败处理

- 校验失败：优先检查 checksum/signature、备份文件是否被截断、`APP_SECRET_KEY` 是否与原环境一致；不要尝试导入未通过校验的文件。
- 数据库不可用：检查 `docker compose ps`、PostgreSQL healthcheck、`DATABASE_URL` 和卷挂载。
- 媒体文件缺失：确认 `data/storage/` 是否完整复制；再运行离线修复。
- 头像或群名缺失：确认机器人在线后打开相关会话，或运行离线修复补缓存。
- 自动备份失败：立即把计划设为 `off`，查看 `.backup-jobs/*.status.json`、`BACKUP_ROOT/failures.log` 与 worker 日志；不要把隐藏 `.incomplete`/`.failed` 改名为正式备份。

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


## Unified archive deployment check

After deployment, verify `/health` and `/api/version` before enabling background synchronization. The version response must show the expected build commit, schema version `20260705_012`, frontend asset version, and QQNT adapter version. Historical identity/profile/media repair must run in dry-run mode first and preserve `message_source_records`, original media references, and profile change records.
