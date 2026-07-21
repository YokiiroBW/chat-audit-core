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


## QQNT ?????

GUI ????????????QQNT ???????????? 16 ?? ASCII
passphrase ???????? API Token ?????? Windows ??? DPAPI
???????? `collector.toml`?

CLI ?????

```powershell
python -m collector --config collector.toml credential set qqnt_database_key
python -m collector --config collector.toml probe C:\path\to\nt_msg.db --snapshot
python -m collector --config collector.toml scan C:\path\to\nt_msg.db --mode initial
```

?? `SQLite header 3` / `QQ_NT DB` ???Collector ????????? WAL/SHM
?????????????? 1024 ????? QQNT SQLCipher ???????
?? QQ ???????????????? `DB_KEY_INVALID`?
## QQNT key helper

The Windows GUI Settings page accepts both the QQNT data directory and the QQ install directory. Use **?? QQ ????** to save a PowerShell helper. The helper validates both paths, refuses to continue while QQ is running, locates the newest `versions\<version>\resources\app\wrapper.node`, downloads the upstream extractor to a temporary file, and runs it only after the user starts the generated script.

The helper does not contain the API Token or database key, does not write collector credentials, and does not modify or upload the QQ database. The GUI generates both a `.ps1` file and a matching `.bat` launcher. The `.bat` file can be double-clicked, or the displayed PowerShell command can be pasted into a terminal. It requires an Internet connection when executed because the upstream extractor is downloaded at that time. Review the downloaded extractor and back up important data before use.
## Multiple computers

The collector source identity is scoped by QQ account and device. Home and office computers therefore keep independent local cursors and upload queues. The server canonicalizes the same QQNT event by its stable account/chat/message identity, so two devices do not create duplicate visible messages; each source record remains available for audit and replay.

The import service also retries one item after a database uniqueness race. This protects the first simultaneous upload of the same event from becoming a dead-letter item. Do not copy a configured `server.source_id` between computers; let the collector derive the source identity from each device configuration.
