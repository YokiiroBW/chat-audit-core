# QQNT Collector import contract v1

This contract is the implementation boundary between a future Windows QQNT
Collector and Chat Audit Core. Changes require a schema/API version review.

## Stable identities

- Import source identity: `source_type + account_id + device_id`.
- Default source id: SHA-256 of the colon-separated source identity.
- QQNT message source identity:
  `qqnt:{account_id}:{chat_type}:{conversation_id}:{msg_id}:{msg_random}:{msg_seq}`.
- `message_id`: SHA-256 of the QQNT message source identity.
- Every Collector batch item must provide `message.message_id`; timestamp/text
  fallback identities are forbidden because parser upgrades must remain replayable.
- `source_record.platform_message_id` carries QQ's raw platform message id when
  available. Chat Audit Core uses it as an alias to deduplicate the same message
  across QQNT and NapCat regardless of arrival order. The SHA id remains stored as
  the QQNT source alias for reply lookup and audit provenance.
- Collector batch ids should be client-stable so create requests can be replayed.
- Exact message chunks are deduplicated by a canonical SHA-256 request hash.
- Integer fields persisted through SQL `Integer` columns must fit the signed
  32-bit range (`-2147483648` to `2147483647`); non-negative counters, media
  ordinals, sizes, durations, and dimensions additionally require a value of 0
  or greater.

## Media states

Valid combinations are:

| Source state | Archive state |
|---|---|
| `downloaded` | `complete`, `failed` |
| `not_downloaded` | `metadata_only`, `thumbnail_only` |
| `missing` | `metadata_only`, `thumbnail_only` |
| `unknown` | `failed` |

The normal video upgrade path is:

```text
not_downloaded + metadata_only
→ not_downloaded + thumbnail_only
→ downloaded + complete
```

`downloaded + complete` is terminal for automatic imports. A later source scan
must not downgrade a NAS archive because the Windows QQ cache was removed.

`complete` requires an existing, non-empty `MediaAsset`. `thumbnail_only`
requires an existing, non-empty thumbnail asset. `not_downloaded` cannot point
to a complete asset. No zero-byte or explanatory placeholder file may be used
to represent an unavailable QQNT media file.

## Collector API

```text
POST /api/import/sources
POST /api/import/batches
POST /api/import/batches/{batch_id}/messages
POST /api/import/batches/{batch_id}/complete
POST /api/import/batches/{batch_id}/fail
```

All endpoints use the existing admin token/session authentication and require
the `operator` or `admin` role. A batch message request accepts 1–500 items.
Domain failures are isolated per item; a successful item is not rolled back by
another item's parse or asset-reference failure.

## Backup and audit

- New exports use `chat-audit-core.backup.v3`.
- Imports remain compatible with `chat-audit-core.backup.v1` and
  `chat-audit-core.backup.v2`.
- V2 preserves import sources, batches, raw source records, and structured media
  references. V3 additionally preserves message direction and creation time,
  media content SHA-256, complete avatar-cache metadata, ordered message parts,
  identity aliases, and profile-change history.
- `not_downloaded` is an informational state and is not a missing-media error.
- Offline repair never fabricates missing media content. It may rebuild an
  index only from a real file whose content hash can be calculated.
