# chat\-audit\-core 修复队列

配套《chat\-audit\-core 代码审计报告》。这是一份可按序执行、可勾选的修复 backlog:自上而下推进即可,每项标注了对应审计编号、动作、文件:行号、依赖关系与粗略工作量(S≈\<1h / M≈半天 / L≈1 天以上)。分四个波次:**W0 发布阻断** → **W1 数据/功能/崩溃(P0)** → **W2 安全与一致性(P1)** → **W3 健壮性/性能/迁移(P2)** → **W4 低危与清理(P3)**。

> **进度看这份文档的勾选状态。** 阶段交接见 `HANDOFF_2026-07-26-B.md`(最新现状、复现命令、陷阱清单)。当前进度:W0 / W1 / W1b / V1–V3 / FQ\-14 / FQ\-15 / FQ\-27 已完成,**下一步建议 FQ\-19(媒体下载 SSRF)**。

> 关键提示:W1 中先做两项"地基重构"(FQ\-2 前缀集中、FQ\-3 身份/方向统一),它们是 H1、H3、M1 等多个 bug 的共同漂移根源,先抽取再改能一次性收敛。

## W0 · 发布阻断 —— 已完成(结论修正)

- [x] **FQ\-0 · H4 结论更正:原判定为误报,W0 无阻断项** —— `app.min.js`(143 KB)、`app.min.css`(52 KB)、`favicon.svg` 在仓库中**一直存在**。此前"核实"用的是云端暂存副本,而我当初只暂存了 `app.css`/`app.js`,`ls` 自然看不到 min 产物——属验证方法有循环依赖,非代码缺陷。已用 `minify_static_assets.py` 重新生成比对,**产物与源码完全同步**。

- [x] **FQ\-0b · 附带发现并修复:`--check` 是空转** —— `scripts/minify_static_assets.py` 从不解析 argv,`--check` 被静默忽略、脚本照常改写文件并 exit 0。实测:把 `app.min.js` 写成 `CORRUPTED` 后 `--check` 仍返回 0 并把文件覆盖回去。已实现真正的 `--check`(只读比对、不写盘、不同步则 exit 1、未知参数报错),并采用文本模式比较使其对 CRLF/LF 不敏感。这同时让 CI 那条 `--check` 从"假门禁"变成真门禁,且因不再写盘,其后的 `git diff --exit-code` 不会再被跨平台换行改写触发。

## W1 · 数据丢失 / 功能错误 / 进程崩溃(P0)—— 已完成

全部 13 项已实施并交付回仓库。逐项如下(括号内为实际改动位置)。

- [x] **FQ\-1 · R6 存储前缀集中** —— 新增 `app/storage_paths.py` 作为单一事实源(`known_storage_prefixes` / `is_local_storage_path` / `count_local_storage_paths` / `stored_media_pattern`),替换 `api.py:509`、`query_service.py`、`import_service.py`(8 处)、`message_service.py`(2 处)全部硬编码 `/static/storage/`。仓库内 `app/` 硬编码前缀归零。
- [x] **FQ\-2 · R1 身份/方向统一** —— 新增 `app/qqnt_identity.py`,把 40020/40021/40030/40033 的语义收敛成单一实现,`query_service` 的两处推导改为调用它。**注意**\:`collector/` 以 PyInstaller 独立打包且 spec 明确 `excludes=["app"]`,无法 import 服务端模块,故采集器保留自有副本,已在两侧文档注释中互相指认,需同步修改。
- [x] **FQ\-3 · H1 读路径不再写脏列** —— `query_service.py:503` 改为仅在确有派生值时用 `set_committed_value` 附加(不标脏),`_attach_parts` 的历史回复覆盖层改为**换入分离副本**而非改写已加载行。双保险:即使写回机制失效,也不会再出现把 `is_outgoing` 抹成 NULL。
- [x] **FQ\-4 · H3 群聊方向** —— `collector/parsers/message.py`\:40020/40021 推导移入 `chat_type == "private"` 分支,群聊分支原有的 `sender_qq == account_id` 判定恢复可达。
- [x] **FQ\-5 · H2 语义去重防误吞** —— `import_service.py` 增加"候选不得已存在同 `source_id` 但不同 `(source_table, source_primary_key)` 的源记录"约束,并在两侧 `source_sequence` 均非空时纳入等值(候选为 NULL 时仍允许跨来源合并)。
- [x] **FQ\-6 · H9 room\_id NameError** —— `offline_repair_service.py:199` 改 `file_name=f'{user_id}.jpg'`。
- [x] **FQ\-7 · H7 采集器崩溃** —— `uploader.py` 在 try 之前绑定 `batch_id = None`;并给 `scheduler.run_forever` 的单轮 `run_once` 加 try/except,单次异常不再终结守护进程。
- [x] **FQ\-8 · H8 死信阻塞投递** —— `queue.py` 就绪门放行 `status NOT IN ('completed','dead_letter')`,消息可带"媒体失败"引用先行投递。
- [x] **FQ\-9 · H5 多机器人重复行** —— 新增 `_robot_message_join(robot_id)`,把 robot 条件并入 ON 子句,替换全部 **7 处** `outerjoin(RobotMessage, ...)`。
- [x] **FQ\-10 · H12 parts 媒体对位** —— 前端按"媒体类 part 计数器"配对(而非全段落 ordinal),并在循环后补渲染未消费引用、防重复渲染;后端新增 `MessageService.link_message_parts_to_media()` 并在导入与实时两条链路的媒体引用建立**之后**调用,真正写入 `media_reference_id`。
- [x] **FQ\-11 · H10 登录门** —— 引入单例 `requestAdminApiToken()` 共享 Promise,并去掉恒为空的 `state.adminApiToken` 判据;顺带修复"退出"按钮永久禁用(改判 `authIdentity`)。
- [x] **FQ\-12 · H11 切换账号竞态** —— `switchAccount` 增加 `accountLoadToken`,赋值/异常/finally 三处均校验令牌。
- [x] **FQ\-13 · H6 备份签名(已重新定级)** —— 详见下节说明,实现为**可选开关**而非默认拒绝。

### FQ\-13 的定级修正

审计原判"无签名包被判定有效属安全缺陷",但源码中存在一条**显式命名**的测试 `test_backup_service_validate_allows_unsigned_legacy_package`,断言"配置了 signing\_key 且包未签名 → 仍然 valid"。这说明允许无签名旧包导入是**刻意的兼容决定**,不是疏漏。直接改默认会破坏该产品保证。

因此改为:新增 `Settings.backup_import_require_signature`(默认 `false`,已写入 `.env.example`),贯穿 `validate_import_package` / `preview_import_package` / `import_package`,并在两个导入端点接线。开启后,缺 checksum、缺签名、或有签名但无密钥可验,一律判为无效并拒绝。既保留旧包可导入,又把审计要求的控制权交给运维。

## W1b · `DB_VFS_UNSUPPORTED` 停止码(自 W4 提前,已完成)

- [x] **FQ\-49a · L18 前半:停止码补齐 \+ 可读性不再靠猜** —— 原问题:该停止码**全仓代码从不产出**,而装了 `sqlcipher3`(即按 `collector/requirements.txt:4` 正常配置)并填任意 16 字节密钥时,`key_provider.py` **只校验密钥长度、从不打开数据库**就返回 `valid`,于是托盘报"ready / 可读取"、扫描器就绪门放行,与 `CODEX_HANDOFF.md` 声明的安全边界不符。

  **先做了可行性实验再定方案**(结论写在这里以免后人重做):用合成 SQLCipher 库实测,**前缀复制完全不可行**——8 KiB / 32 KiB 前缀下 `PRAGMA page_count` / `schema_version` / `user_version` / `freelist_count` 和 `SELECT count(*) FROM sqlite_master` **全部失败**;只有全量剥头才能打开。也就是说"实测可读"必然要付整库复制代价(真实库约 410 MB),**不能放进每次兼容性检测**(`gui/app.py:65` 在 GUI 启动时就会调用)。

  **实现**:把"允许尝试"与"已实测可读"拆开。新增 `KeyValidationResult.status == "unverified"` 及 `may_attempt` / `requires_key` 两个属性;`usable` 收紧为"确实打开成功过",密钥长度检查再也不能让它为真。判定顺序为 runtime → 密钥门 → 剥头可行性门,**密钥门刻意排在前面**(用户只是还没填密钥时应得到可操作的 `needs_key`,而非笼统的"不支持");新增 `sqlcipher.body_looks_encrypted()` 做零复制、零解密、免密钥的可行性检查(自定义头后面是明文 SQLite 体 → `DB_VFS_UNSUPPORTED`)。真验证落在扫描器**本来就会真实打开**剥头副本的那一层(`scanner.py:187`),失败以 `ScanIssue` 上报,零额外成本。

  **托盘**:`readable_count` 只统计已证实可读的库;新增 `needs_verification` 状态,文案为"能否读取将在首次同步时实测确认"。**该状态仍允许启动**(`SYNCABLE_COMPATIBILITY_STATUSES`)——拦在托盘等于永久禁用 QQNT 单来源部署,而可读性只能由真实打开来证明。改动:`collector/qqnt/{sqlcipher,key_provider}.py`、`collector/sync/scanner.py`、`collector/app/main.py`、`collector/gui/controller.py`,并同步更正 `CODEX_HANDOFF.md` 的两处描述。新增 2 条测试(合成 SQLCipher 库,按 `sqlcipher_available()` 跳过),全量 **295 passed**。

  **剩余**:L18 的另一半(`uploader.py:232` 的 `local_path` `.get` 兜底)仍在 FQ\-49。

## W2 · 安全与一致性(P1)

- [x] **FQ\-14 · M2 修复全部乱码** —— 已完成。损坏只有一种形态:**CJK 字符被逐个替换成 ASCII 问号**(某次以 ASCII/`errors='replace'` 写盘),字节已丢失,**不能靠重新解码还原**。全仓无 latin\-1/gbk 型乱码,也无 U+FFFD 替换字符,所有文件都是合法 UTF\-8。

  **根因定位到单一提交**\:`1b4c1c6 feat: integrate QQNT import and chat console UI` 一次性引入全部乱码(各文件计数 0 → N),其后 `2d66a87` 又在 `app.js` 上追加 6 处。

  **恢复依据(不是猜)**\:`app.js` 的 `打开会话：` / `刷新会话：` / 分隔符 `·` 直接取自 `1b4c1c6^` 与 `2d66a87` 的删除行;`QQ号：` 取自同文件 779/874 行既有写法(无空格,正好 2 字);`QQ好友` 对应 776 行私聊兜底名;`选择 QQ 安装目录` 对照 594 行同构标题与 300 行字段标签;`开机自动启动仅支持 Windows` 对应 `gui/app.py:331` 复选框标签(9 字精确吻合)。README 两节由 `1b4c1c6` 新增时即已损坏,**git 无干净祖先,按当前代码行为重写**(含 W1b 之后的三个停止码与 `needs_verification` 语义)。

  **实际改动**\:`app.js`(5 行,并重新生成 `app.min.js`)、`collector/gui/app.py`(7 处)、`collector/gui/controller.py`(1 处)、`tests/test_collector_parsers.py`(2 处夹具)、`README.md` 与 `collector/README.md` 两节重写。

  **哨兵**\:新增 `scripts/check_text_encoding.py`(照 `minify_static_assets.py` 骨架,纯 ASCII 源码,非 ASCII 标记一律用 `chr()` 构造,以免自我误报),检查 UTF\-8 可解码性、U+FFFD 替换字符、latin\-1/gbk 特征串、以及**连续 ≥2 个问号**。已接入 CI 的 `Verify text encoding` 步骤(排在资产门禁**之前**),并由 `tests/test_deployment_files.py` 两条测试锁定:一条跑全树、一条钉死边界规则。

  **两条规则上的取舍,改哨兵前务必先读**\:(1) JS 空值合并运算符的豁免**只对 `.js` 生效**——`README.md` 的 `16` \+ 两个问号 \+ ` ASCII` 长得与它一模一样,若对 `.md` 也豁免就会漏报真实损坏;(2) **不检查单个问号**——SQL 占位符、URL query、正则字符类 `[!?，。]` 都合法,单问号规则误报率过高,代价是 `app.js:845` 那种单字损坏(`'我'` → 一个问号)哨兵抓不到,只能靠 review。

  **三处原判被推翻**\:`query_service.py:145-154` 预览占位**早已是正确中文**,无需改动;`.gitattributes` 最后一行**不是乱码**(合法 UTF\-8,真实问题是 7\-13 行的 `export-ignore` 全部指向不存在的文件,归 FQ\-50);`collector/gui/*` 的 `_ui_text(r"\u...")` **也不是乱码**,是刻意的 ASCII 安全写法(`app.py:23` 运行时 `decode("unicode_escape")`),显示正确,本轮未动。另:审计给的 `app.js` 行号已因 W0/W1 漂移,且**新发现** `collector/README.md`(9 行,最大一块)、`collector/gui/*`、`tests/` 三处审计未列的损坏。
- [x] **FQ\-15 · R4 账号范围下沉 \+ M3 回复预览范围** —— 已完成。

  **下沉**\:新增 `app/message_scope.py`(与 `storage_paths.py` / `qqnt_identity.py` 同一模式)作为"这个账号能看到哪些消息"的单一事实源,导出 `robot_message_join` / `robot_message_scope`,并新增 `apply_robot_message_scope(stmt, robot_id)` 一次性同时施加 JOIN 与 WHERE ——**只加其中一个正是 H5 的成因**,给出组合函数就是为了让那种写法不再可能。`query_service.py` 的 7 处改为引用共享定义,`offline_audit_service.py` 一并复用。

  **M3(安全)**\:`_attach_reply_previews` 此前**既无账号范围也无房间范围**,只按 `external_message_id` 全局回查,而该列在 `models.py:98` 仅有索引、**无任何唯一约束**,承载的是 OneBot 的每连接小整数消息 ID ——跨账号、跨房间重号是常态而非边界情况。旧实现的字典还是**顺序未定义的后写覆盖**,于是把别的账号/别的房间的消息原文与发送者昵称渲染进了预览:轻则预览错乱,重则泄露调用方根本打不开的会话内容。现改为按 `(room_id, external_message_id)` 复合键,并用 `apply_robot_message_scope` 限定账号、用 `room_id.in_(本页房间集)` 限定房间;别名回查(`message_source_records`)同样处理;同键多行时按 `timestamp desc, msg_hash desc` 取最新,消除未定义顺序。回复天然指向同房间的消息,所以房间是身份的一部分,不是可选收窄。

  **R4 顺带修好的一致性缺陷**\:`offline_audit_service.py:128` 原先是 `RobotMessage` 的**内连接**,而 QQNT 导入的消息根本没有 `robot_messages` 行 —— 纯 QQNT 账号做离线审计时会扫到 0 条并报告"一切就绪"。改用共享 scope 后与查询层口径一致。

  **回归证据(前三条都先用旧算法实测确认修复前会选错)**\:新增 5 条测试。跨账号同 ID(实测旧代码选中了另一账号的机密正文)、**同账号跨房间**同 ID(旧代码选中了另一个房间 —— 这条证明光有账号 scope 不够)、**同房间跨账号**别名回查(两个账号各自与 QQ 7001 的私聊共用同一 `room_id`,只有账号 scope 能区分 —— 这条证明光有房间过滤也不够)、**跨房间搜索页**同一 ID 在一个房间可直接命中、在另一个房间只能走别名(见下)、以及离线审计能扫到纯导入消息且不越过账号边界。

  **实现中途自查发现并修掉的一个二次缺陷**\:待解析集合若按裸 ID 求差,会出现"该 ID 已在 A 房间直接命中 → 认定已解析 → B 房间的别名回查根本不执行"的静默丢预览。跨房间搜索页真会触发。故改为全程按 `(room_id, reply_id)` **二元组**求差与回填。

  **同规则但不在 R4「五处」清单内、仍待决策的 4 处**\:`api.py:1584`(`_attach_local_forward_payload`,**会写库**)、`capture_policy_service.py:123,145`、`backup_service.py:459`。它们都是 `RobotMessage` 内连接,属**覆盖不足**(看不到纯导入消息)而非泄露,与 M3 的性质不同;改动会分别影响转发本地化、抓取目标列表与**导出包范围**,后者是数据导出口径决策,故本轮未擅自扩大,单列于此。
- [ ] **FQ\-16 · M4 实时链路语义去重** —— `message_service.py:30-73` 增加与导入侧一致的语义兜底层(可与 FQ\-2/FQ\-5 复用)。工作量 M。
- [ ] **FQ\-17 · M5 单条摄入身份归一化** —— `message_service.py:143-155` 落库键改用 `canonical_room_id/sender_id` 并写 IdentityAlias(或从 schema 移除该二字段)。工作量 S。
- [ ] **FQ\-18 · R2 统一头像补全器 \+ M6** —— 合并 `api.py` 三个 `_hydrate_missing_*` 与 `ws.py` 三个后台任务为一个"per\-item 隔离 \+ `[:20]` 上限 \+ 后台化"补全器;修好 `/bots`/`/messages` 无隔离与私聊无上限(`api.py:1023`)。工作量 M。
- [ ] **FQ\-19 · M7 SSRF DNS rebinding** —— `media_service.py:287-310,657` 校验后钉住已解析公网 IP 直连(连 IP \+ 设 Host 头)。工作量 M。
- [ ] **FQ\-20 · M8 会话/令牌 TTL** —— `AdminSession` 增 `expires_at`(迁移 \+ 模型),`match_session`/`match_token` 校验并将过期置 revoked。工作量 M。
- [ ] **FQ\-21 · M9 卡片 HTML 存储型 XSS** —— `.html`/`.svg` 类媒体(`api.py:367-379`)强制 `Content-Disposition: attachment` 或独立无凭据子域,附加严格 CSP。工作量 M。
- [ ] **FQ\-22 · R7 原子写盘 \+ M10** —— 抽 `atomic_write_bytes()`(temp\+`os.replace`),用于 `backup_service.py:683` 自动备份与 `:996-998` 嵌入媒体还原。工作量 S。
- [ ] **FQ\-23 · R9 路径包含校验 \+ M11 resolver 根约束** —— 抽 `path_within(root, p)` 公共函数,补进 `collector/media/resolver.py:26-49`,拒绝越出 QQ 数据根的媒体路径;无 md5 的协议媒体不接受单体积匹配。工作量 M。
- [x] **FQ\-24 · M14 is\_outgoing 幂等统计** —— **代码在 W0/W1(`1fd1797`)已顺带修好**,当时未勾选。`origin/main` 上确为 `if message_data.is_outgoing is not None:` 无条件置 `changed`,现分支已是先比较再置。本轮补上缺失的回归:既有幂等测试走的是**同批次重放**短路,根本到不了更新分支,故新增「同一来源、第二个批次、完全相同的消息」用例断言 `updated == 0`。
- [ ] **FQ\-25 · M15 离线审计 covered\_urls 错位** —— `offline_audit_service.py:163-172` 改用 `ordinal→url` 映射比对(`_parse_cq_media_segments_with_ordinals`)。工作量 S。
- [ ] **FQ\-26 · M16 回填队头饿死** —— `media_backfill_service.py:248-262` 引入推进游标/失败退避标记;清理冗余 `contains` OR;为 `media_assets.local_path` 建索引(顺带修性能)。工作量 M。
- [x] **FQ\-27 · M17 备份 cron** —— 已完成,两条路都走了(实现 \+ 文档),因为原问题不只是"支持的表达式太少"。

  **完整故障链**\:`next_run_from_cron` 只接受 `M H * * *` 的纯数字形式,而 `DISASTER_RECOVERY.md:15` 让运维"提高 `AUTO_BACKUP_CRON` 频率"。照做写 `0 */6 * * *` → 抛 `ValueError` → `_auto_backup_loop` 的外层 `except Exception` 捕获 → 记日志 \+ **append** 一条 `failures.log` \+ `sleep 60` → **永远循环**。后果有三层:自动备份**一次都不再执行**;`failures.log` 每天多 1440 行、无上限增长;而 `GET /api/backup/status` 仍然返回 `enabled: true`,Web 控制台显示"开启",**运维完全看不出备份已经停摆**。

  **实现**\:新增 `_parse_cron_field`,分钟与小时字段支持 `*`、数字、`a-b`、`*/步长`、`a-b/步长` 及逗号列表;日/月/星期仍必须为 `*`(即只支持"每天重复")。**刻意拒绝 `5/10`**——各实现对它的含义不一致(是"5 起每 10"还是"就是 5"),猜错等于在运维没要求的时间点跑备份,不如明确报错。

  **止血**\:cron 解析失败从"外层通吃"里拆出来单独处理,按 cron 值去重,只在**值变化时**报一次(日志 \+ `failures.log`),仍继续轮询以便改回正确值后自动恢复、无需重启。顺带修了外层 except 记的是 `settings.auto_backup_cron`(环境值)而非实际生效值——数据库覆盖优先,原来会指向运维根本没设过的配置。

  **可见性**\:`BackupStatusResponse` 增加 `cron_error` 与 `next_run_at`。`enabled` 只表示"开关打开",不代表能跑,所以坏 cron 时前端状态行显示"开启(计划无法执行)"并用 `.bad` 样式,附一行具体错误;正常时显示"下次运行"时间,改完频率能立刻确认是否生效。

  **文档**\:`DISASTER_RECOVERY.md` 写明受支持的 cron 子集与不支持的例子,并把 RPO 那条的建议具体化为 `0 */6 * * *`。

  **回归**\:4 组测试 —— DR 文档里的各种子日频率均可解析、`0 */6` 跨午夜正确进位、9 种无法调度的表达式一律抛错、以及 `_auto_backup_loop` 跑 5 轮后 `failures.log` **只有 1 条**记录(修复前是每轮 1 条),外加 API 层的 `cron_error` 断言与前端契约断言。
- [ ] **FQ\-28 · M18 /metrics 鉴权** —— `main.py:293` 的 `/metrics` 纳入 admin 鉴权或独立监听;404(`main.py:159,177`)归并为固定标签 `unmatched`,避免无界基数与 actor 泄露。工作量 S。
- [ ] **FQ\-29 · M19 X\-Forwarded\-For 信任** —— `api.py:219-223` 仅在配置了可信代理时解析 XFF(uvicorn `--proxy-headers` \+ 信任列表)。工作量 S。
- [ ] **FQ\-30 · M20 默认端口绑定** —— `docker-compose.yml:39-41` 默认绑 `127.0.0.1:8000`,第二端口 25810 移入可选 override;文档标注公网需反代 \+ TLS。工作量 S。
- [ ] **FQ\-31 · M21 采集器脱敏** —— `logging.py:13-14` 日志脱敏复用 `config.SENSITIVE_CONFIG_KEYS` \+ `_token/_secret/_key` 后缀匹配;`diagnostics.py:103` / `scanner.py:170` 写入诊断的路径做 basename/哈希化(去 QQ 号)。工作量 M。
- [ ] **FQ\-32 · M22 采集器 TLS 告警** —— `config.py:192` / `uploader.py:53,66`\:非回环 `http://` 或 `verify_tls=false` 时在 UI/日志显式告警。工作量 S。
- [ ] **FQ\-33 · M26 去重键 MD5→SHA256** —— `message_service.py:22-28,90-121` 消息去重键/媒体主键改用 SHA\-256(需迁移与回填规划)。工作量 L。
- [ ] **FQ\-34 · M27 修复脚本** —— `repair_qq_identity_and_duplicates.py:12` 移除硬编码 QQ 号默认值(改为必填参数);`:77` 的 `DELETE ... USING` 做方言适配或显式标注"仅 PostgreSQL"。工作量 S。

## W3 · 健壮性 / 性能 / 迁移 / 打包(P2)

- [ ] **FQ\-35 · M12 采集器只读一致性** —— 普通库默认走快照 \+ `immutable=1`(`reader.py:80-92`);自定义 VFS 剥头后映射 `-wal`(`sqlcipher.py:29-52`),或文档明确"仅已落盘"语义并缩短回扫窗口。工作量 L。
- [ ] **FQ\-36 · M13 队列批量事务** —— `queue.py:52,110` / `state.py` 提供批量入队 API,`scanner` 每页单事务 `executemany`,消除逐条 fsync。工作量 M。
- [ ] **FQ\-37 · M23 GUI 线程安全** —— `gui/app.py:663,647-651` 进 `_run_task` 前于主线程取 Tk 变量值再传纯数据(参照 `auto_detect`)。工作量 S。
- [ ] **FQ\-38 · M24 protobuf 递归深度** —— `parsers/protobuf.py:156-191` 加 `max_depth` 参数(参照 `forward.py`),防畸形 reply 中断整库扫描。工作量 S。
- [ ] **FQ\-39 · M25 迁移守护 \+ 索引/约束漂移** —— 增"旧库全链迁移后与全新库逐表 diff"守护测试;统一索引名(`ix_` vs `idx_`);为旧库补 `content_sha256` 唯一约束(先清重)与 `avatar_status` NULL 回填。位置 `helpers.py`、`database.py:62-63`、`models.py:70,85,101,137`。工作量 M。
- [x] **FQ\-40 · M28 采集器打包图标** —— **审计结论已过时**:该图标在 `2d66a87` 就已加入仓库并被 `git ls-files` 跟踪(5682 字节、合法 ICONDIR、含 7 种尺寸),两份 spec 的 `icon=` 一直指向真实文件。本轮只补上缺失的测试:校验文件存在、ICO 头合法、且两份 spec 都仍引用它 —— 因为图标缺失只会在打包那一刻炸,那是最不该发现问题的时机。
- [ ] **FQ\-41 · M29 前端加载失败提示** —— `app.js:2941-2942` 增 `roomLoadError` 状态,`renderChat` 空列表时区分"确无消息"与"加载失败\+重试"。工作量 S。
- [ ] **FQ\-42 · M30 前端渲染增量化** —— `renderChat`(`app.js:1458-1546`)按 `messageKey` 做增量 diff(新增 append、状态变化局部更新),避免轮询打断媒体播放/丢选区;媒体筛选下轮询沿用合并策略(`:3075-3077`)。工作量 L。
- [ ] **FQ\-43 · M31 GUI 主线程阻塞** —— stop/exit(`controller.py:544`)异步化;状态刷新(`app.py:550,587`)移后台线程回填、`initialize()` 只启动一次、缓存 `has_api_token`。工作量 M。
- [ ] **FQ\-44 · M32 双开机自启** —— GUI 勾选自启时接管/移除 `installer.iss:38` 的 `{userstartup}` 快捷方式,自启只保留单一来源。工作量 S。
- [ ] **FQ\-45 · 性能:导入批量预取** —— `import_service.py:311-651` 块级先批量预取 `Message`/`RobotMessage`/`MessageSourceRecord`/`MessageMediaReference` 到内存字典,再逐条决策,替代每条 10\+ 次串行往返。工作量 L。
- [ ] **FQ\-46 · 性能:离线服务读盘** —— `offline_repair/audit` 分块哈希(`hashlib.file_digest`)、合并两轮遍历、`repaired_paths` 改 set,消除大文件 OOM 与 O(n²)。工作量 M。

## W4 · 低危与清理(P3)

- [ ] **FQ\-47 · 后端低危批次** —— L1 `ws.py:279` receive\_json 隔离;L2 `ws.py:52` token 用 `secrets.compare_digest`;L3 `query_service.py:447` 游标退化 `<=`;L4 `api.py` 限流桶清理;L5/L6 `message_service.py` media 回查兜底 \+ `@staticmethod` \+ 清残留 part;L7 搜索大小写/昵称取最新/众数确定性;L8 `import_service` upsert\_source 更新丢弃 \+ failed\_media 口径;L9 离线审计范围一致 \+ 截断标注;L10 淘汰无盐 SHA256 密码 \+ 审计值级脱敏。工作量 M。
- [ ] **FQ\-48 · 前端低危批次** —— L11 `app.js:2430` json\=null 防御 \+ 单条渲染 try/catch;L12 表情 `data.raw` 类型收敛;L13 删死字段引用 \+ 合并排序纳入 `source_sequence`;L14 删死代码 \+ 历史"到头"标记;L15 删 `app.css:1962-2010` 覆盖调色盘的第二套主题 \+ 清重复选择器;L16 `sw.js` 仅缓存 2xx \+ 版本号随 minify 自动注入。工作量 M。
- [ ] **FQ\-49 · 采集器低危批次** —— L17 `message.py` msg\_random 回退 rowid \+ file\_size int32 校验 \+ 状态降级合法性;L18 `uploader.py:232` local\_path `.get` 兜底 \+ `DB_VFS_UNSUPPORTED` 停止码与文档对齐;L19 `controller.py` 恢复异常上报 \+ 关停 loop 保护 \+ 迁移清源目录 \+ 非 Windows `windll` 守卫。工作量 M。
- [ ] **FQ\-50 · 部署/仓库卫生** —— L20:`.gitignore` 补 `data/database/*` 与 `*.sqlite3-*`(防 WAL/SHM 入库);`vendor/wheels` 二进制转 Git LFS 或构建期按校验和下载;`alembic.ini timezone=UTC` 补 `python-dateutil` 依赖;`.dockerignore` 补 `.tmp`。工作量 S。
- [ ] **FQ\-51 · 剩余复用重构** —— R3 Profile 服务基类合并;R5 `apply_message_cursor` 抽取;R8 采集器来源 ID 哈希/调度装配去重;前端报表行/管理列表/媒体元素构造抽公共函数(可删 \~120 行)。工作量 M。
- [ ] **FQ\-52 · 测试补强** —— 补:同一消息并发投递的主键冲突路径(PG 形态)、端到端恢复流(备份→validate→import→audit→repair)、alembic×lightweight 等价、前端契约/`sw.js` 资产清单一致性;`test_alembic_cli` 增 PostgreSQL 覆盖。工作量 L。

## 一次性核实项(非代码修复,但阻塞"可发布"判断)—— 已全部完成(2026\-07\-26 本机核实)

- [x] **V1 · 依赖版本** —— **两个版本都真实存在**:`pip index versions` 显示 `fastapi` 有 0.139.2(当前最新 0.140.0),`starlette` 有 1.3.1(即当前最新)。"starlette 从 0.47 跳到 1.3.1 幅度异常"只是错觉——starlette 确实发布过 1.0 正式版,0.52.1 之后就是 1.0.0。**组合兼容已验证**:`fastapi 0.139.2` 的元数据声明 `starlette>=0.46.0`(无上界),`pip install --dry-run --ignore-installed -r requirements-prod.txt` 完整解析成功(exit 0),`pip check` 无破损依赖。`pip-audit -r requirements-prod.txt` 结果为 **No known vulnerabilities found**。已把 `pip check` 与 `pip-audit`(独立 venv,避免扰动钉定版本)纳入 `.forgejo/workflows/ci.yml`。
- [x] **V2 · 仓库卫生核实** —— `git ls-files` 确认 `NUL`、`$root/`、`wechat_tray_adapter/` **均未被跟踪**,`collector.toml` 已被 `.gitignore:47` 忽略。**`$root/` 不是敏感数据**:递归仅 2 项,是 `data` 和 `Tencent QQ` 两个**空目录**(来自未展开的 shell 变量),无任何文件。`NUL`(101 字节)内容是一行 SSH known\_hosts 记录(`[192.168.31.210]:77` 的 ed25519 **主机公钥**,公开信息,非凭据)。`wechat_tray_adapter/` 仅剩 `__pycache__`,其源码在 `8b93076 发布：主分支移除微信路线` 中已刻意移除,历史可查。**结论:无需 `git filter-repo` 清历史。** 三者已删除(`NUL` 需走 `\\?\` 扩展路径),并补 `.gitignore` 的 `NUL` / `$root/`,`git status` 已不再列出它们。
- [x] **V3 · Dockerfile 构建物** —— 已验证存在且被跟踪:`data/storage/.gitkeep`、`data/backups/.gitkeep`(Dockerfile:32\-33 只引用这两个)、`vendor/wheels/imageio_ffmpeg-0.6.0-py3-none-manylinux2014_x86_64.whl`。审计快照缺失确系快照过滤二进制所致,非仓库缺陷。

## 执行建议

W2 各项相互独立,可并行分工;W3/W4 可排入后续迭代。每完成一个波次,建议补一条错题本记录并跑一次 `pip-audit` 与 `python scripts/minify_static_assets.py --check`(现已是真门禁)。

## W0/W1 交付与验证状态

**已改动文件**(均已写回仓库):新增 `app/storage_paths.py`、`app/qqnt_identity.py`;修改 `app/config.py`、`app/api.py`、`app/services/{query,message,import,backup,offline_repair}_service.py`、`app/static/assets/app.js`(\+ 重新生成的 `app.min.js`)、`collector/parsers/message.py`、`collector/sync/{queue,uploader,scheduler}.py`、`scripts/minify_static_assets.py`、`.env.example`。

**已完成的验证**\:150 个 Python 文件 `py_compile` 全通过;`app.js` 与生成的 `app.min.js` 通过 `node --check`;`storage_paths` 与 `qqnt_identity` 两个新模块以桩件隔离单测,覆盖前缀判定、正则替换、以及 H3 的关键回归(群聊行 40020≠40021 但发送者是他人 → 正确返回 `False`);H12 的 ordinal 配对逻辑以等价 Python 实现验证了五种排列(文字\+图片、文字\+图\+视频、图片在前、显式 id、孤儿媒体);`--check` 门禁以"故意损坏 min 文件"实测确认能返回 1。改动后逐文件复核 diff,并把被脚本改写的行尾还原(`import_service.py`、`backup_service.py`、`config.py` 原为 CRLF),使 diff 只剩真实改动。

**回归已于 2026\-07\-26 在本机补跑完成(此前"未运行"的状态已解除)**\:

| 环境 | 结果 |
| --- | --- |
| 本机 `.venv`(fastapi 0.116.1 / starlette 0.47.3) | **293 passed**,exit 0 |
| 钉定生产版本(fastapi 0.139.2 / starlette 1.3.1 / pydantic 2.13.4) | **293 passed**,exit 0 |
| `python scripts/minify_static_assets.py --check` | exit 0 |

两套依赖都跑是必要的:`requirements-dev.txt` 第一行是 `-r requirements-prod.txt`,即 **CI 实际装的是生产钉定版本**,而本机 `.venv` 已漂移到 0.116.1 / 0.47.3。只在本机 venv 上跑绿**不能**代表钉定栈可用,尤其 starlette 跨了 0.47→1.3 的主版本边界。钉定栈用独立 venv 验证(未改动本机 `.venv`),唯一告警是 `starlette.testclient` 提示 httpx 将来要换 httpx2,不影响结果。W0/W1 的 13 项修复在两套栈上均无回归。

首轮唯一失败项 `test_collector_qqnt_reader.py::test_database_key_validation_and_cursor_repository` 已修,**它不是 W0/W1 引入的**,详见下节。

**行尾问题已核实:不存在 CI 变红风险,原判过度担心。** `git ls-files --eol app/static/assets/` 显示 `app.min.js` / `app.min.css` 在**索引里是 LF**(`i/lf`),工作区 CRLF(`w/crlf`)只是本机 `core.autocrlf=true` 的检出转换结果,`attr/` 为空。而 `minify_static_assets.py:42` 用的是 `path.write_text()`(平台默认换行),并没有硬编码 CRLF——所以 Linux CI 上生成 LF、索引也是 LF,`git diff` 本来就是干净的。"minified 产物是 CRLF、我按原约定输出 CRLF"这句是对脚本行为的误读。

## 2026\-07\-26 对审计前提的三处更正

**更正一:首轮唯一的测试失败是"测试过时",不是代码缺陷。** `test_collector_qqnt_reader.py:228` 断言自定义 VFS 库必须返回 `unsupported` / `DB_SQLCIPHER_UNAVAILABLE`,实测返回 `invalid` / `DB_KEY_INVALID`。根因:该断言**写死了"本机没有 SQLCipher runtime"这个环境前提**,而 `collector/requirements.txt:4` 恰恰要求 `sqlcipher3>=0.6.2`(本机已装 0.6.2)。有 runtime 时 `key_provider.py:38-49` 走的是密钥门,测试传的 `"unused"` 只有 6 字节,判 `DB_KEY_INVALID` 是**正确行为**。也就是说这条测试只在"未按 requirements 装依赖"的机器上才通过。已改为按 `sqlcipher_available()` 分支断言,两种环境都成立,并顺带把三重能力门钉住(缺密钥 / 非 16 字节 → 不可用;16 字节 → valid)。`collector/qqnt/` 无本地改动、最后一次提交早于本轮,已排除 W0/W1 关联。

**更正二:剥头读取链路早已实现,`qq-import-alternatives-eval-2026-07-20.md:233` 已过时。** 那句"即使本机存在 sqlcipher3,仍明确返回 `DB_VFS_UNSUPPORTED`,因为当前项目尚未提供 QQNT 1024 字节头适配"的前提已不成立:`sqlcipher.py:29` 的 `materialize_clear_database`(剥 1024 字节)与 `configure_connection`(page\_size 4096 / kdf\_iter 4000 / HMAC\_SHA1 / PBKDF2\_HMAC\_SHA512)已接入 `reader.py:71-79`、`scanner.py:182`、`profiles.py:63`、`app/main.py:402`。当前 `validate()` 的行为符合 `qqnt-direct-read-research-2026-07-20.md:186-191` 的三重能力门规格,**不是缺陷**。读这两份文档时必须注意先后顺序。

**更正三(需决策,已升级关注):`DB_VFS_UNSUPPORTED` 在全仓代码中从不产出。** 全仓 grep 确认该停止码只出现在文档与审计报告里,`collector/` 无一处产生它。但 `CODEX_HANDOFF.md:106` 声称"当前 Collector 会精确返回 `DB_VFS_UNSUPPORTED`",`:225` 更把"不要绕过 `DB_VFS_UNSUPPORTED`"列为硬性禁止项。实际后果:装了 `sqlcipher3`(即按 requirements 正常配置)且填了任意 16 字节密钥时,`key_provider.py:45-49` **只校验密钥长度、从不打开数据库**就返回 `valid`,于是 `gui/controller.py:405` 把该库计入 `readable` 并让托盘报告"ready / 可读取",`sync/scanner.py:125` 的就绪门也放行。这与 `CODEX_HANDOFF.md:122` 描述的"托盘会显示缺少兼容 NT VFS 并阻止启动真实数据库同步"不符。审计已记为 L18 / FQ\-49 并排在 W4,**但它落在项目自己声明的安全边界上,建议提前处理**;由于三重能力门是刻意设计,是"补齐停止码与托盘文案"还是"收紧为必须实际打开成功才算 readable",属产品决策,本轮未擅自改动。

**更正四:CI 从未使用过 `--check`,"CI 里那条 `--check` 是假门禁"的说法不准确。** `.forgejo/workflows/ci.yml`(注意是 Forgejo Actions,仓库无 `.github/`)原第 49\-50 行是 `python scripts/minify_static_assets.py` 后接 `git diff --exit-code`,并没有传 `--check`——这套"重新生成 \+ diff"其实是有效门禁。FQ\-0b 修好 `--check` 本身仍然值得(不写盘、失败信息更明确),本轮已把该步骤替换为 `--check` 单条命令。
