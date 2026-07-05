# Migration Scripts

This directory is Alembic-ready but does not require Alembic at runtime yet.

Current production startup still uses `app.database.LIGHTWEIGHT_MIGRATION_REGISTRY`.
The files in `migrations/versions/` mirror that registry one-to-one so future
Alembic adoption can move from the same ordered version chain instead of
reconstructing history later.

