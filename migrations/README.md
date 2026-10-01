# Migration Scripts

This directory is the Alembic migration home.

Production startup still runs `app.database.LIGHTWEIGHT_MIGRATION_REGISTRY` as
a compatibility fallback, while Alembic provides an explicit CLI path for manual
and deployment-time upgrades.

The files in `migrations/versions/` mirror the lightweight registry one-to-one
through their `lightweight_version` field. Alembic `revision` values stay short
enough for the default PostgreSQL `alembic_version` table. The migrations are
intentionally idempotent: a fresh database is initialized to the current schema,
and legacy databases can still receive the known compatibility columns.

Typical usage:

```bash
DATABASE_URL=sqlite+aiosqlite:///./data/chat_audit.sqlite3 python -m alembic upgrade head
python -m alembic current
```
