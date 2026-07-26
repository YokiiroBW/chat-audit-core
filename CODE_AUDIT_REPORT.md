# chat\-audit\-core 代码审计报告

审计日期:2026\-07\-26 · 审计对象:仓库全部源码(`app/` 后端与前端、`collector/` 采集器、`migrations/`、`scripts/`、`tests/`、部署配置、文档)。

本次审计以人工全量走读为主:先做基线静态检查(148 个 Python 文件 `py_compile`、前端 JS `node --check` 全部通过,0 语法错误),再按模块分工深度审查,最后由主审对全部高危发现逐条回读源码复核。下列所有"已核验"标记的发现,均由主审直接在源码对应行确认无误,非推测。

## 审计范围与方法

范围覆盖六大块:后端核心(`api.py` / `main` / `config` / `database` / `models` / `schemas` / `ws` / `adapters`)、后端服务层(`app/services/` 22 个文件)、原生 JS 前端(`index.html` / `app.js` 3797 行 / `app.css` / `sw.js`)、Windows 采集器(`collector/` 的 parsers / qqnt / sync / media / gui)、部署与迁移(Docker / Alembic / CI / scripts)、以及 README·docs 宣称功能与源码的逐条核对。

分级标准:🔴 **高危**\=数据丢失 / 安全 / 明确功能错误 / 部署阻断;🟠 **中危**\=边界条件 / 一致性 / 潜在故障 / 性能;🟡 **低危**\=健壮性 / 可维护性;🔵 **优化与复用**。

## 执行摘要

整体判断:功能主线完整、测试基数大(292 个测试函数)、既往安全整改(媒体鉴权、SSRF 私网拦截、HttpOnly Cookie、WS 单事件隔离)大多真实生效。但本轮审计发现了**若干会造成静默数据丢失、方向判定系统性错误、以及一处部署即白屏**的严重问题,这些是发布前必须优先处理的。

| 级别 | 数量 | 性质 |
| --- | --- | --- |
| 🔴 高危 | 12 | 静默数据丢失 × 3、功能错误 × 2、部署阻断 × 1、安全 × 1、进程崩溃/投递丢失 × 2、前端可用性/契约 × 3 |
| 🟠 中危 | \~32 | 一致性漂移、SSRF/XSS/鉴权边界、非原子写盘、只读承诺、性能与批量化 |
| 🟡 低危 | \~30 | 健壮性、乱码文案、脆弱测试、仓库卫生 |
| 🔵 优化/复用 | 10\+ | 三份方向推导、两份补全逻辑、profile 服务合并、CSS 死规则等 |

**必须最先修复的 6 项**\:(1) H1 读消息接口静默把 `is_outgoing` 写成 NULL;(2) H3 采集器群聊方向全部判错;(3) H2 导入语义去重误吞同秒同文消息;(4) H4 前端 min 产物缺失导致白屏;(5) H7 采集器 uploader 崩溃拖垮守护进程;(6) H8 媒体死信永久阻塞消息投递。其中 H1 与 M1(前缀漂移)叠加后会在默认配置下**每次打开会话必然触发**,应作为第一优先级。

## 一、高危发现(🔴)

### H1 读消息接口把 `is_outgoing` 改写为 NULL 并经头像补全 commit 持久化(已核验)

**位置**\:`app/services/query_service.py:503`;触发链 `app/api.py:1067` → `app/services/user_profile_service.py:56`(`await db.commit()`)。

`outgoing_by_hash` 只由 `MessageSourceRecord` 构建,而该表仅 QQNT 导入才写入。第 503 行 `message.is_outgoing = outgoing_by_hash.get(message.msg_hash)` 对 **NapCat 实时采集的消息**(无源记录)会取到 `None`,把数据库里已存的 `True/False` 覆盖为 `None`,使 ORM 对象标脏。`GET /api/messages` 紧接着调用头像补全,一旦任一发送者头像需刷新,`upsert_user_profile` 的 `commit()` 就把这些脏改动持久化为 `NULL`。同函数 `_attach_parts`(:570\-576)对已加载 `MessagePart` 的 `text_content`/`part_type` 也有同类改写,会被同一次 commit 写库。

**后果**\:机器人自己发出的消息(`is_outgoing=True`)所在会话被打开一次,该页所有实时消息的收发方向被永久清空、不可恢复,前端"我方消息靠右"失效。两个独立子审计各自发现,主审已核验。

**修复**\:读时派生值放非映射属性或响应 DTO;仅当 `msg_hash in outgoing_by_hash` 时才覆盖;头像补全使用独立会话,杜绝读路径 commit。

### H2 QQNT 导入语义去重误合并"同源、同秒、同内容"两条真实消息(已核验)

**位置**\:`app/services/import_service.py:454-471`。

语义兜底层按 `平台+会话+类型+发送者+秒级时间戳+文本` 匹配,**不排除**已绑定同一 `source_id` 但 `source_primary_key` 不同的候选,也不比对 `source_sequence`。群里同一人一秒内发两条相同文本(如"哈哈""\+1")时,第二条前四层未命中后落入语义层匹配到第一条,其 `MessageSourceRecord` 正常新建,但第二条消息实体**永不入库** → 静默丢消息。`scripts/repair_qq_identity_and_duplicates.py` 的合并键有相同缺陷。

**修复**\:语义层增加"候选不得已存在来自同一 `source_id`、`source_primary_key` 不同的源记录"约束;两侧 `source_sequence` 均非空时纳入等值条件。

### H3 采集器群聊消息方向 `is_outgoing` 被系统性判为"本账号发送"(已核验)

**位置**\:`collector/parsers/message.py:112-115`。

`40020/40021` 是私聊(c2c)方向字段,但推导写在 `if chat_type == "private"` 分支**之前**无条件执行。群聊行 `40020`\=发送者 Uid、`40021`\=群 Uid,二者几乎恒不相等 → `is_outgoing` 恒为 `True`;群聊分支(:128)本应生效的 `sender_qq == account_id` 因 `is_outgoing` 已非 `None` 成为**死代码**。服务端 `message_service` 直接落库该值。

**后果**\:全部群消息(含他人发送的入站消息)都被标为本账号发送,审计视图"谁发的"整体错误。作者显然本意是让该规则仅对私聊生效(群聊分支已单独写了正确判定)。

**修复**\:把 `40020 != 40021` 推导收敛进 `private` 分支;群聊只用 `sender_qq == account_id`。

### H4 前端压缩产物与图标缺失 → 部署即白屏、Service Worker 安装必败、CI 守护失效(已核验)

**位置**\:`app/static/index.html:7,8,524`、`app/static/sw.js`、`.forgejo/workflows/ci.yml:50`、`Dockerfile:29`。

`static/assets/` 目录实际只有 `app.css`、`app.js`,而 `index.html` 引用的 `app.min.css`、`app.min.js` 与 `favicon.svg` **均不存在**,`Dockerfile` 也不在构建期执行 minify。以当前仓库构建镜像:三个资源 404 → 页面无样式无脚本白屏;`sw.js` 的 `cache.addAll` 因任一 404 整体 reject → SW 永远装不上。CI 的 `git diff --exit-code app/static/assets/app.min.*` 对**未跟踪文件恒返回 0**,守护形同虚设;`tests/test_web_console.py` 断言这三文件 200,当前树必然失败。

**修复**\:把 min 产物与 favicon 纳入版本库,CI 改用 `test -f` \+ 重新生成后 diff;或在 Dockerfile 构建阶段直接执行 `scripts/minify_static_assets.py`。

### H5 多机器人场景下 `outerjoin(RobotMessage)` \+ OR 范围致消息列表重复行、`limit` 失真

**位置**\:`app/services/query_service.py:185-198`(范围)、`421-430`(list\_messages)、`286-296`、`626-627`。

`outerjoin(RobotMessage)` 的 ON 条件不含 `robot_id`,一条消息关联 N 个 robot 就产生 N 个连接行;当该消息又满足 OR 的第二支(被本账号 ImportSource 导入过)时,其它 robot 的连接行也通过 WHERE → 同一消息**重复出现**。`list_messages` 不去重,重复行直接进响应并挤占 `limit`;`list_rooms` 的窗口被稀释。触发:两个账号同群、跨来源合并后,浏览时每条消息显示多遍、分页错乱。

**修复**\:把 robot 条件并入 JOIN 的 ON(`and_(RobotMessage.msg_hash==Message.msg_hash, RobotMessage.robot_id==robot_id)`),或改 EXISTS 子查询,四处统一。

### H6 备份导入 checksum/签名"存在才校验",无签名包被判定有效并放行(已核验)

**位置**\:`app/services/backup_service.py:1026-1049`;`import_package:1255-1264`。

`checksum` 与 `signature` 均为 `if ... is not None` 才校验;二者都缺失时不追加 `errors`,`report["valid"]` 仍为 `True`,导入放行。checksum 用无密钥 SHA\-256(可伪造),真实性只依赖 HMAC 签名,而签名从不被强制。诱导管理员导入一个"结构合法、无签名"的备份包即可注入/覆盖 canonical 消息与媒体资产,破坏审计不可抵赖性(端点有 admin 保护降低暴露面,但"安全控件存在却不生效")。

**修复**\:生产默认开启强制签名;配置了 `signing_key` 而包内无 `signature`/`checksum` 时判定无效并拒绝。

### H7 采集器 uploader 建批次失败分支引用未绑定的 `batch_id` → 守护进程崩溃(已核验)

**位置**\:`collector/sync/uploader.py:338-400`。

`batch_id` 仅在 `try` 内的 `_create_writable_batch` 绑定。若该调用抛 `CollectorApiError`(如服务端 5xx `SERVER_UNAVAILABLE`),进入 `except` 后 `mark_retry` 已把消息安全置 retry,但随后 `logger.warning(extra={"batch_id": batch_id})` 触发 **UnboundLocalError**;该异常非 `CollectorApiError`,经 `scheduler.run_once` 重抛 → `run_forever`(无 try)→ `_run_forever`(无 try)→ `asyncio.run` 退出 → **采集守护进程崩溃需人工重启**。把常见的可恢复错误升级成了致命崩溃。

**修复**\:函数开头 `batch_id = None`;并给 `run_forever` 的单轮 `run_once` 加 try/except 兜底。

### H8 媒体进入 dead\_letter 后父消息被就绪门永久阻塞、静默不投递、无指标、无自动恢复(已核验)

**位置**\:`collector/sync/queue.py:184-192`(就绪门)、`247-261`(mark\_retry→dead\_letter)。

消息只有在其全部媒体 `status='completed'` 时才可出队;而 `mark_retry` 在 `attempts>=max_attempts` 或 `retryable=False` 时把媒体置 `dead_letter`(≠completed)→ 就绪门 `NOT EXISTS(status!='completed')` 永假 → 该消息**永远无法上传**;消息本身停留在 `pending`(attempts\=0),不计入 dead\_letter 指标运维无从察觉;`requeue_dead_letter` 只处理 `status='dead_letter'` 的行,不解冻被阻塞的消息。触发:暂存文件被提前删除(`MEDIA_SOURCE_MISSING`,retryable\=False,max\_attempts\=1)或服务端持续拒收 → 该(可能仅一张失败图的)消息永久丢失投递。

**修复**\:就绪门放行 `status IN ('completed','dead_letter')`(消息引用已能表达 `archive_state=failed`);或引入把死信媒体降级 metadata\_only 后释放消息的 reaper。

### H9 离线修复用户头像下载引用群循环遗留变量 `room_id`,无群消息时 NameError 全量回滚(已核验)

**位置**\:`app/services/offline_repair_service.py:199`。

用户循环里 `file_name=f'{room_id}.jpg'` 用的是上一个群循环的遗留变量。纯私聊归档(无群消息)时群循环未执行 → `NameError`,且发生在唯一 `commit` 之前 → 本次修复的媒体索引、占位头像**全部丢失**。有群时不崩溃但文件名语义错误(取最后一个群号)。

**修复**\:改为 `file_name=f'{user_id}.jpg'`。

### H10 前端 401 处理:`state.adminApiToken` 恒空致提示风暴、登录后仍重复弹窗(已核验)

**位置**\:`app/static/assets/app.js:219-226`(配合 125/2827/2839)。

登录已改走 HttpOnly Cookie,`state.adminApiToken` 只被赋值为 `''`,从未存入真实 token,故 `canReuseNewToken` 恒为 false。未登录初始加载并发 4 个 GET → 用户连续面对最多 4 个阻塞式 `window.prompt`;会话过期时 5 秒轮询每周期再弹一次。此外"退出"按钮 `disabled = !state.adminApiToken`(:903)因此**永久禁用**,Cookie 会话用户无法从 UI 登出。

**修复**\:引入单例"登录门"(共享 Promise),后续 401 等待同一 Promise,登录成功后仅凭 Cookie 重放;去掉 `Boolean(state.adminApiToken)` 条件;退出按钮改判 `authIdentity`。

### H11 前端切换账号竞态:`switchAccount` 无请求令牌/中止保护,旧响应覆盖新状态

**位置**\:`app/static/assets/app.js:2518-2544`(对照 `selectRoom` 有 `roomLoadToken` \+ `AbortController` 双保险)。

`switchAccount` 在 `await Promise.all([...])` 之后不校验当前账号是否仍是发起时的账号。快速点击 A→B 且 A 的响应后到时,`state.roomList`/`captureTargetList` 被 A 的数据覆盖,而顶栏显示 B → 会话列表与账号错配(5 秒轮询后才可能纠正)。

**修复**\:仿 `selectRoom` 增加 `accountLoadToken`,赋值前校验 `state.currentRobot.id === acc.id`;或对旧请求 `abort()`。

### H12 结构化消息 parts 媒体对位错误 \+ `media_reference_id` 后端从不写入,归档媒体与状态卡片丢失

**位置**\:`app/static/assets/app.js:1596-1605`;后端佐证 `app/services/message_service.py:353-369`(`_upsert_message_parts` 从不设 `media_reference_id`)。

主路径 `media_reference_id` 在库中恒为 NULL,前端永远走 `mediaByOrdinal` 回退;但 `part.ordinal` 是**全段落序号**、`reference.ordinal` 是**媒体内序号**,凡媒体不在消息首段(如"文字\+图片")查表必然 miss → 归档完成的本地资产、以及带 `availability_reason` 的"源端未下载/归档失败"区分卡片**不渲染**,且未消费引用被静默丢弃。这直接削弱本平台核心的审计区分展示(前端六个 `availability_reason` 分支与后端取值一致,却因对位失败而看不到)。

**修复**\:短期在前端为 parts 维护"媒体类 part 计数器"查表并在循环后追加未消费引用;长期在后端 `_upsert_message_parts` 写入真实 `media_reference_id`。

## 二、中危发现(🟠)

以下按模块归并;"已核验"表示主审已回读源码确认。

| 编号 | 模块 | 位置 | 问题与触发 | 建议 |
| --- | --- | --- | --- | --- |
| M1 | 后端 | api.py:509;query\_service.py:152,160;import\_service.py:549,575 | **(已核验)** 头像"已落地"判定与导入头像状态硬编码 `/static/storage/`,而默认前缀是 `/media` → 每次列表请求头像补全永不收敛,并**放大 H1** 使 `is_outgoing` 每次读取都被擦写;导入头像状态永远判 `pending` | 统一读 `settings.public_storage_prefix`,兼容两套已服务前缀 |
| M2 | 后端/前端/文档 | query\_service.py:121,145\-154;app.js:830\-831,1526,2937,3151;README:196\-207 | **(已核验)** 源码中文字面量 mojibake:预览显示 `[鍥剧墖]` 等、私聊标签 `? QQ??`、日志 `?????`、README「QQNT」整节乱码 | 以 UTF\-8 恢复正确中文;CI 加乱码哨兵 |
| M3 | 后端 | query\_service.py:586\-603 | **(已核验)** 回复预览回查无账号/房间范围,OneBot 数字 ID 跨机器人碰撞 → 错误预览 \+ 跨账号内容泄露 | 回查加 `room_id` 与 `_robot_message_scope` |
| M4 | 后端 | message\_service.py:30\-73 | 实时入库缺语义去重层,"先导入后实时"顺序产生重复消息(反向可合并,不对称) | 增加与导入侧一致的语义兜底 |
| M5 | 后端 | message\_service.py:143\-155 | 单条摄入端点丢弃 `canonical_sender_id/room_id`,不做身份归一化 → 私聊会话分裂 | 落库键改用 canonical 值并写 IdentityAlias |
| M6 | 后端 | api.py:491\-503,1023,1097\-1109 | 头像补全在 `/bots`、`/messages` 无 per\-item 隔离(rooms 已修)可击穿列表;私聊补全无上限,`/rooms` 内串行无界外网请求(准 DoS) | 统一 per\-item try/except \+ rollback \+ `[:20]` 上限,并移入后台任务 |
| M7 | 后端 | media\_service.py:287\-310,657 | DNS Rebinding(TOCTOU)SSRF:校验 host 名后 httpx 再次独立解析,两次解析间可变 | 校验后钉住已解析公网 IP 直连 |
| M8 | 后端 | admin\_user\_service.py:156\-175;models AdminSession | 会话/令牌无 TTL,泄露后长期有效直到显式吊销 | 增 `expires_at` 并在鉴权时校验 |
| M9 | 后端 | media\_service.py:807\-839;api.py:367\-379 | 攻击者可控卡片 HTML 同源提供,CSRF Cookie 非 httponly → 存储型 XSS 可读 token | `.html`/`.svg` 强制 `Content-Disposition: attachment` 或独立无凭据子域 \+ CSP |
| M10 | 后端 | backup\_service.py:683,996\-998 | 自动备份文件、嵌入媒体还原非原子写盘,崩溃残留损坏包被当作最新备份 | temp \+ `os.replace` 原子落盘(复用 `save_media_asset` 模式) |
| M11 | 采集器 | media/resolver.py:26\-49 | **(已核验)** 媒体路径未约束在 QQ 数据根内,协议媒体无 md5 时仅体积门 → 构造消息可令采集器读取并上传任意本地文件(越界外泄) | `_valid_file` 增加根目录包含校验;无 md5 不接受单体积匹配 |
| M12 | 采集器 | reader.py:80\-92;sqlcipher.py:29\-52 | 默认 `direct_readonly` 未设 `immutable`、无一致快照,"绝对只读 \+ WAL 一致"承诺默认不成立;自定义 VFS 不套 `-wal` 致最新未落盘消息缺失 | 普通库默认走快照 \+ `immutable=1`;自定义库剥头后映射 `-wal` |
| M13 | 采集器 | queue.py:52,110;state.py:234\-249 | 队列逐条独立 `BEGIN IMMEDIATE` 事务(错题本旧性能问题未解),初次全量导入 fsync 密集 | 提供批量入队 API,单事务 `executemany` 写一页 |
| M14 | 后端 | import\_service.py:493\-496 | **(已核验)** `is_outgoing` 非空即 `changed=True`,重放/重扫全部误报 `updated`,幂等统计失效 | 改为先比较值再置位 |
| M15 | 后端 | offline\_audit\_service.py:163\-172 | `covered_urls` 用"槽位序号"索引"仅远程段"紧凑列表 → 混合媒体消息误报/漏报 | 用 `ordinal→url` 映射比对 |
| M16 | 后端 | media\_backfill\_service.py:248\-262 | **(已核验)** 回填候选恒按最旧排序,永久失败消息占满窗口饿死新消息;`contains("http")` 冗余;全表 LIKE 扫描 | 引入推进游标/失败退避;清冗余;`local_path` 建索引 |
| M17 | 后端 | backup\_service.py:697\-712;DISASTER\_RECOVERY.md:15 | **(已核验)** 备份 cron 只支持"每日 HH:MM",DR 文档却建议提频 → 配 `0 */6 * * *` 会使自动备份循环永久失败 | 实现受限 cron 子集或修文档 |
| M18 | 部署 | main.py:159,177,293;metrics.py:106 | `/metrics` 无鉴权 \+ 404 用原始路径作标签(无界基数)\+ 导出 `actor` 名 → 未认证信息泄露 \+ 内存增长 | `/metrics` 纳入鉴权;404 归并固定标签 |
| M19 | 部署 | api.py:219\-223 | 无条件信任 `X-Forwarded-For` → 限流绕过、审计 IP 污染、限流字典无界增长 | 仅在配置可信代理时解析 XFF |
| M20 | 部署 | docker\-compose.yml:39\-41 | 双端口默认绑定 `0.0.0.0` 无 TLS,平台存私人聊天 | 默认 `127.0.0.1`,第二端口移入 override |
| M21 | 采集器 | logging.py:13\-14;diagnostics.py:103;scanner.py:170 | **(已核验)** 诊断包 `status.json` 含完整库路径中的 QQ 号/非用户盘路径;日志脱敏键集窄于配置拦截键集且仅精确匹配 | 路径 basename/哈希化;日志复用 `SENSITIVE_CONFIG_KEYS` \+ 后缀匹配 |
| M22 | 采集器 | config.py:192;uploader.py:53,66 | 允许 `http://` 明文且 `verify_tls` 可关,Bearer Token 可经明文/不校验通道外发,无告警 | 非回环 http 或关校验时显式警告 |
| M23 | 采集器 GUI | gui/app.py:663,647\-651 | **(已核验)** worker 线程内 `.get()`/`_settings_value()` 直读 Tk 变量,违反 Tkinter 线程模型(Windows 上可抛/静默错) | 进 `_run_task` 前于主线程取值再传纯数据 |
| M24 | 采集器 | parsers/protobuf.py:156\-191 | reply 预览相互递归无深度上限,畸形消息 `RecursionError` 被宽泛 except 捕获 → 整库本轮扫描中止 | 加 `max_depth`(参照 forward.py) |
| M25 | 迁移 | helpers.py:9;database.py:62\-63;models.py:101,137,70,85 | **(已核验)** 迁移\=`create_all` 快照式、三处同步维护无历史固化;索引命名漂移(`ix_` vs `idx_`)致旧库 `content_sha256` 缺唯一约束;`avatar_status` 旧库 NULL vs 模型 NOT NULL | 增"旧库全链迁移后与全新库逐表 diff"守护测试;统一索引名;补 UPDATE 回填 |
| M26 | 后端 | message\_service.py:22\-28,90\-121 | 消息去重键/媒体主键用 MD5,审计留证平台可被哈希碰撞"吞消息" | 换 SHA\-256 |
| M27 | 脚本 | repair\_qq\_identity\_and\_duplicates.py:12,77 | **(已核验)** 硬编码真实 QQ 号作默认参数(隐私);`--apply` 用 PG 专有 `DELETE ... USING`,SQLite 崩溃 | 移除硬编码;方言适配或标注仅 PG |
| M28 | 采集器打包 | packaging/\*.spec:27,32 | **(已核验)** 引用的 `chat-audit-collector.ico` 不存在,PyInstaller 构建失败 | 补图标或移除 `icon=` |
| M29 | 前端 | app.js:2941\-2942,3149\-3162 | 会话加载失败被吞,界面显示"暂无消息" → 审计场景误导为"确无记录" | 区分"确无消息"与"加载失败\+重试" |
| M30 | 前端 | app.js:3075\-3081,1458\-1546 | 媒体筛选下轮询整体替换列表致翻页历史被截断;轮询全量重渲染打断语音/视频播放、丢文本选区 | 筛选沿用合并;renderChat 增量 diff |
| M31 | 采集器 GUI | controller.py:544;app.py:550,587 | stop/exit 主线程 `join` 冻结 UI≤5s;状态刷新每 3 秒主线程做重 I/O 含一次 DPAPI 解密 | 停止异步化;状态快照移后台线程 \+ 缓存 |
| M32 | 采集器打包 | installer.iss:38 \+ controller.py:190\-198 | 安装器 `{userstartup}` 与 GUI 注册表 Run 双自启冲突,登录弹"已在运行" | 自启只保留单一来源 |

## 三、低危发现(🟡)

| 编号 | 模块 | 位置 | 问题 |
| --- | --- | --- | --- |
| L1 | 后端 | ws.py:279 | `receive_json()` 解码异常未纳入单条隔离,单个畸形帧断开整条 WS |
| L2 | 后端 | ws.py:52 | OneBot token 用 `==` 非常量时间比较,query 传参易入日志 |
| L3 | 后端 | query\_service.py:447\-448,419 | 复合游标缺省回退用严格 `timestamp <`,第三方分页漏同秒消息;around 定位仅 `timestamp+1` |
| L4 | 后端 | api.py:96,258\-269 | 限流桶 `_RATE_LIMIT_BUCKETS` 只增不删,内存缓慢增长 |
| L5 | 后端 | message\_service.py:117\-126 | `save_media_asset` 冲突回查仅按 sha256,历史 NULL 行触发 RuntimeError |
| L6 | 后端 | message\_service.py:337\-369 | `_upsert_message_parts` 缺 `@staticmethod`;缩短片段后残留高序号旧部件 |
| L7 | 后端 | query\_service.py:629\-637,286\-289,343 | 搜索 LIKE 大小写双后端不一致;昵称取字典序最大非最新;众数平票不确定 |
| L8 | 后端 | import\_service.py:182\-199,363 | `upsert_source` 并发冲突静默丢弃本次更新;消息失败混入 `failed_media` 统计口径污染 |
| L9 | 后端 | offline\_audit\_service.py:126\-131 | 审计账号范围只认 RobotMessage,与查询四处不一致;5000 截断无标注 |
| L10 | 后端 | admin\_user\_service.py:59\-63;audit\_log\_service.py:13\-25 | 兼容无盐 SHA\-256 旧密码校验;审计脱敏仅按键名,值内敏感串不脱敏 |
| L11 | 前端 | app.js:2430\-2438 | `[CQ:json,data=null]` → TypeError 中断整个消息列表渲染 |
| L12 | 前端 | app.js:2269\-2271 | `data.raw` 为对象时表情仍可显示 `[object Object]`(NapCat 主路径已防) |
| L13 | 前端 | app.js:841,2993\-3000 | 引用后端不存在字段 `avatar_file_hash`/`profile_updated_at`;合并排序忽略 `source_sequence` 致同秒顺序漂移 |
| L14 | 前端 | app.js:1690\-1698,3170\-3200 | `renderMessageContent` return 后死代码;历史翻页无"到头"标记 \+ 双重渲染 |
| L15 | 前端 CSS | app.css:1962\-2010 | 文件尾第二套主题覆盖调色盘变量,暗色模式调色盘失效 \+ 大量重复选择器死规则 |
| L16 | 前端 SW | sw.js:30\-49 | 缓存非 2xx 响应;版本号三处手工同步,漏 bump 即永久旧资源 |
| L17 | 采集器 | message.py:72\-77,226\-227,326\-327 | `msg_random` 恒 0 去重碰撞;`file_size` 未按 int32 上限校验;缩略图失败降级 metadata\_only 属非法转移 |
| L18 | 采集器 | uploader.py:232;key\_provider.py:38\-49 | artifact 响应 `local_path` 无防御(KeyError);`DB_VFS_UNSUPPORTED` 停止码文档要求但代码从不产出 |
| L19 | 采集器 GUI | controller.py:108\-109,542\-543,172;app.py:783 | 恢复逻辑裸 except 静默;关停窗口对已关闭 loop 调用;迁移是复制残留 DPAPI 副本;非 Windows `windll` 无守卫 |
| L20 | 部署/卫生 | .gitignore;Dockerfile.ffmpeg:8;alembic.ini:4 | 缺 `data/database/*` 与 `*.sqlite3-*` 忽略(WAL/SHM 可能被提交);`vendor/wheels` 数十 MB 二进制入库;`alembic.ini timezone=UTC` 缺 `python-dateutil` 依赖 |

> 说明:主审所在快照未见简报提及的根目录 `NUL` 文件、`$root/`(疑似真实 QQ 用户数据)与 `wechat_tray_adapter/` 残留。若真实仓库存在:`NUL` 为 Windows 保留名会破坏 checkout(高·可用性),`$root/` 疑似敏感数据入库(高·隐私),均需删除 \+ 补 `.gitignore` \+ 视情况清理历史。请在本机 `git ls-files` 核实。

## 四、前后端复用与代码优化方案(🔵)

审计发现多处"同一逻辑三份实现"正是若干高危问题的漂移根源,优先做下列抽取即可同时消除 bug 与重复:

1. **统一 QQNT 身份/方向推导(修 H1、H3 根源)**\:`40020/40021/40030` 收发方向与对方 QQ 归一化目前散落在 `collector/parsers/message.py`、`query_service.py`、`scripts/repair_qq_identity_and_duplicates.py` 三份,规则一致但极易漂移。抽 `app/qqnt_identity.py` 单一实现,读写与修复共用;读时派生方向严禁改写持久 ORM 列。

2. **统一头像补全器(修 M6)**\:`api.py` 三个 `_hydrate_missing_*`(bots/rooms/messages)与 `ws.py` 三个后台任务逻辑重复且隔离策略不一致。合并为一个"带 per\-item 隔离 \+ 上限 \+ 后台化"的补全器。

3. **Profile 服务合并**\:`user_profile_service` 与 `room_profile_service` 的 `upsert_*`/`cache_qq_*` 近乎逐行相同,`bot_profile_service`、`profile_placeholder_service` 同构。抽象 `ProfileService(identity_type, avatar_url_builder)`,统一"下载失败隔离 \+ 变更记录"。

4. **账号可见范围下沉(修 M3、H5 一致性)**\:`_robot_message_scope` 提为公共 scope,`offline_audit_service`、回复预览回查一并复用,rooms/messages/搜索/定位/审计五处同一规则。

5. **游标谓词 \+ 排序三元组**\:`(timestamp, coalesce(source_sequence,-1), msg_hash)` 在 `query_service.py` 三处重复,抽 `apply_message_cursor(stmt, ...)`。

6. **存储前缀判断集中(修 M1)**\:`/static/storage/` 硬编码散落 `api.py`、`query_service.py`、`import_service.py`,统一读 `settings.public_storage_prefix`。

7. **原子写盘工具(修 M10)**\:抽 `atomic_write_bytes()`(temp \+ `os.replace`)供备份写文件、嵌入媒体还原复用。

8. **采集器共享(修 M11)**\:来源 ID 哈希公式(`main`/`uploader` 各一份)、后台调度装配(`controller._background_main` 与 `main._run_forever` 逐行重复)、流式哈希(`checksum`/`staging`/`snapshot` 三份)与路径包含校验(散落且 resolver 缺失)统一到公共 util。

9. **前端复用**\:报表行渲染(5 处)、管理列表渲染(3 处)、媒体元素构造(2 处)抽公共函数(可删 \~120 行);`renderChat` 增量化替代全量重建(修 M30);删除 `app.css` 尾部被覆盖的第二套主题死规则(修 L15)。

## 五、功能核对(README 宣称功能 vs 源码实现)

README「主要功能」11 条逐条核对,结论均为**已实现**并有 file:line 级证据,唯一偏差是自动备份的 cron 限制(见 M17)。

| \# | 功能 | 实现位置 | 状态 |
| --- | --- | --- | --- |
| 1 | NapCat / OneBot 11 反向连接 | ws.py:244\-253(token 校验 :46\-52) | 已实现 |
| 2 | 多适配器、多 QQ 账号 | ws.py:258,281\-296;adapters CRUD api.py:463\+ | 已实现 |
| 3 | 自动识别机器人身份、换号不混淆 | bot\_profile\_service.py:8\-54;database.py:231\-263 | 已实现 |
| 4 | 群名/头像/私聊头像本地缓存 | ws.py:157\-241;room/user\_profile\_service | 已实现(补全隔离见 M6) |
| 5 | 文本/链接/图片/表情/语音/视频/文件 | media\_service.py:37\-45;前端 app.js:1914\-1947 | 已实现(结构化对位见 H12) |
| 6 | 回复跳转、戳一戳、合并转发 | app.js:2021,3127;/api/forward api.py:1529 | 已实现(戳一戳为展示级) |
| 7 | 图片弹窗预览、滚轮缩放、拖拽 | app.js:3614,3708,3709 | 已实现(依赖 H4 修复后可用) |
| 8 | B 站/小程序卡片解析真实链接 | media\_service.py:65\-91,330\-345 | 已实现(为通用卡片字段提取,措辞略宽) |
| 9 | 黑白名单抓取策略 | capture\_policy\_service.py;message\_service.py:139\-141 | 已实现 |
| 10 | 自动备份/导入/导出/离线检查 | backup\_service.py:1500\+;api.py:1600,1663,1443,1483 | 已实现(cron 仅每日,见 M17) |
| 11 | 默认 SQLite,可切 PostgreSQL | config.py:18;database.py:158\-171 | 已实现 |

`CODEX_HANDOFF.md` 的 Q0–Q10 已完成清单抽查亦与源码相符(导入契约、4×4 媒体状态机、双源去重、backup v2、DPAPI/只读/probe、托盘 GUI)。前次内部审计(`docs/release-final-audit-2026-07-22.md`)的媒体鉴权、SSRF 私网拦截、前端盲目重试三项 P0/P1 已确认修复;Starlette 依赖升级项转为待人工核实(见附录)。

## 六、附录:验证记录、限制与已读清单

**主审已回读核验的高危项**\:静态资源缺失(`ls` 确认仅 app.css/app.js)、mojibake 字面量(query\_service.py 与 app.js)、`is_outgoing` 覆盖 \+ 头像补全 commit 链、采集器群聊方向 message.py:112\-115、导入语义去重 454\-471、offline\_repair room\_id:199、uploader batch\_id:338\-400、backup 校验 1026\-1049、dead\-letter 就绪门 queue.py:184\-192、backfill 排序 248\-262、resolver 无根校验、import is\_outgoing changed\=True 493\-496。全部与报告描述一致。

**环境限制**\:本审计在设备快照上进行(无 `.git`、PyPI 经代理受限),因此**未能实机运行 292 个测试、未能核实 `fastapi==0.139.2`/`starlette==1.3.1` 的可安装性**(`requirements-prod.txt:1-2`,starlette 0.47→1.3.1 跳版异常,请本机 `pip index versions` 核实并将 `pip-audit`/`pip check` 纳入 CI);仓库卫生中 `NUL`/`$root/` 等条目按 `.gitignore` 影响分析,需本机核实。

**基线**\:Python 源文件 148 个 `py_compile` 全通过;前端 `app.js` 3797 行、`app.css` \~73KB、`index.html` 26KB,`node --check` 通过;最大单文件 `backup_service.py`(1550)、`app.js`(3797)、`api.py`(1718)、`media_service.py`(1429)。审计已完整通读 `app/` 全部后端与前端、`collector/` 全部模块、`migrations/` 12 个版本、`scripts/` 4 个、部署配置与 39 个测试文件的结构;参照 `docs/错题本.md` 核对既往修复仍生效。
