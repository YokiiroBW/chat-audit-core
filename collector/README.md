# Chat Audit QQ Collector

Implemented:

- TOML configuration with sensitive-key rejection;
- Windows DPAPI credential storage;
- local SQLite state, cursor, queue, run and parser-failure tables;
- media staging with content hashes and quota enforcement;
- two-phase media/message upload queue;
- exponential retry and dead-letter handling;
- scheduler, CLI status and simulated message import.
- QQNT data discovery, schema probing and read-only paginated access;
- message/media parsing into a persistent two-phase upload queue;
- initial, incremental and reconciliation scans with durable composite cursors.

QQNT `SQLite header 3` / `QQ_NT DB` databases are supported through a
read-only snapshot path: the Collector copies the database family, strips the
1024-byte custom header from the copy, opens it with SQLCipher parameters, and
feeds the numeric QQNT message tables into the existing import queue. The
Collector never modifies the original QQ files, extracts `pskey` values, or
injects code into QQ. Unknown protobuf payloads remain replayable raw Base64;
common QQNT text payloads are decoded into the existing text message format.

## Quick start

For normal Windows use, launch `chat-audit-qq-collector-gui.exe`. The tray app
provides first-run setup, database compatibility checks, queue status, sync
controls, simulated connection testing, dead-letter retry and diagnostics.
Closing its window keeps it running in the notification area.

The CLI remains available for automation and advanced diagnostics:

```powershell
Copy-Item collector\collector.example.toml collector.toml
python -m collector --config collector.toml credential set api_token
python -m collector --config collector.toml credential set qqnt_database_key
python -m collector --config collector.toml simulate
python -m collector --config collector.toml status
python -m collector --config collector.toml run-once
python -m collector --config collector.toml discover
python -m collector --config collector.toml probe C:\path\to\nt_msg.db --snapshot
python -m collector --config collector.toml scan --mode initial
python -m collector --config collector.toml scan --mode incremental
python -m collector --config collector.toml run-once --mode incremental
```

`run` starts with rate-limited initial history batches, persists the composite
cursor after each successful page, then switches to hourly-overlap incremental
scans and daily reconciliation scans. If the process or Windows restarts, the
next run resumes from the local state database. Upload batches remain durable
while the server or NAS is unavailable.

The configuration file must not contain API tokens or QQNT database keys. They
are stored in the current Windows user's DPAPI scope. In the GUI, enter the key
in Settings as a masked field; it is saved only when non-empty and is never
written to `collector.toml`. The key must be the 16-byte ASCII passphrase
returned by the QQNT key extraction step. QQNT reads always use a consistent
snapshot, a stripped temporary copy, SQLCipher, `query_only`, and a
write-denying authorizer.


## QQNT 数据库密钥

GUI 的「设置」页以掩码字段填写 QQNT 数据库密钥，它必须是密钥提取步骤返回的
16 字节 ASCII passphrase。该密钥与 API Token 一样保存在当前 Windows 用户的
DPAPI 作用域，绝不会写入 `collector.toml`。

CLI 等价命令：

```powershell
python -m collector --config collector.toml credential set qqnt_database_key
python -m collector --config collector.toml probe C:\path\to\nt_msg.db --snapshot
python -m collector --config collector.toml scan C:\path\to\nt_msg.db --mode initial
```

对于 `SQLite header 3` / `QQ_NT DB` 数据库，Collector 会连同 WAL/SHM 文件族
生成一致性快照，再把剥离 1024 字节自定义头的临时副本交给 SQLCipher，以
`query_only` 和拒写 authorizer 打开，不会修改 QQ 的原始文件。

停止码与含义：缺少 SQLCipher 运行时返回 `DB_SQLCIPHER_UNAVAILABLE`；缺少密钥
或长度不是 16 字节返回 `DB_KEY_INVALID`；自定义头之后不是 SQLCipher 密文体则
返回 `DB_VFS_UNSUPPORTED`。填入 16 字节密钥只说明格式正确，此时状态为
`needs_verification`，能否读取由首次同步真实打开剥头副本来证明，失败会作为
扫描问题上报，托盘不会仅凭密钥长度声称可读。

## 读取一致性语义

`[qq].read_mode` 决定普通（非自定义头）数据库怎么读，两种取值的语义不同：

- `direct_readonly`（默认）：直接以只读方式打开 QQ 正在使用的文件。**只能看到 QQ 已经落盘的数据**，
  仍在 WAL 里、尚未 checkpoint 的最新消息这一轮扫描读不到，下一轮增量回扫会补上。分页扫描期间
  文件仍在被写入，因此看到的是一个移动中的目标，而不是某一时刻的一致快照。检测到 WAL 非空时，
  托盘运行日志会明确提示本次是这种语义。
- `snapshot_copy`：先连同 WAL/SHM 生成一致性快照再读，读到的是某一时刻的完整状态。代价是每次
  扫描都要复制整个数据库——真实主消息库约 410 MB，按默认同步间隔计算磁盘开销可观。

自定义头（`QQ_NT DB`）数据库**始终**走快照，因为剥头本身就需要完整副本。

## QQNT key helper

The Windows GUI Settings page accepts both the QQNT data directory and the QQ install directory. Use **生成 QQ 解锁脚本** to save a PowerShell helper. The helper validates both paths, refuses to continue while QQ is running, locates the newest `versions\<version>\resources\app\wrapper.node`, downloads the upstream extractor to a temporary file, and runs it only after the user starts the generated script.

The helper does not contain the API Token or database key, does not write collector credentials, and does not modify or upload the QQ database. The GUI generates both a `.ps1` file and a matching `.bat` launcher. The `.bat` file can be double-clicked, or the displayed PowerShell command can be pasted into a terminal. It requires an Internet connection when executed because the upstream extractor is downloaded at that time. Review the downloaded extractor and back up important data before use.
## Multiple computers

The collector source identity is scoped by QQ account and device. Home and office computers therefore keep independent local cursors and upload queues. The server canonicalizes the same QQNT event by its stable account/chat/message identity, so two devices do not create duplicate visible messages; each source record remains available for audit and replay.

The import service also retries one item after a database uniqueness race. This protects the first simultaneous upload of the same event from becoming a dead-letter item. Do not copy a configured `server.source_id` between computers; let the collector derive the source identity from each device configuration.
