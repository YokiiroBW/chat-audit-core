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

Unknown private QQNT protobuf layouts are preserved as replayable raw Base64
records. They are not guessed or presented as successfully decoded messages.
Databases with the real `SQLite header 3` / `QQ_NT DB` header are reported as
`DB_VFS_UNSUPPORTED` until a version-compatible NT VFS runtime is available;
the Collector does not extract `pskey` values or inject code into QQ.

## Quick start

```powershell
Copy-Item collector\collector.example.toml collector.toml
python -m collector --config collector.toml credential set api_token
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
are stored in the current Windows user's DPAPI scope. Database discovery never
opens QQNT files. Probe/read operations use SQLite `mode=ro`, `query_only`, a
write-denying authorizer, and optional consistent snapshots.
