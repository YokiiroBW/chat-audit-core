"""Read-only inventory of credentials still on a legacy shape.

Answers the two questions that decide whether the compatibility code can go:

* how many admin users still carry an unsalted SHA-256 password hash, which is
  accepted today and upgraded to bcrypt the next time that user logs in -- so a
  count of zero means the legacy branch in ``admin_user_service`` can be deleted
  without locking anybody out;
* how many media assets share a ``content_sha256``, and how many profile rows
  still hold a NULL ``avatar_status``, which is what migration
  ``20260705_016`` will merge and backfill.

Reads only. Nothing here writes, so it is safe to point at production.
"""

from __future__ import annotations

import asyncio
import json

from sqlalchemy import text

from app.database import AsyncSessionLocal


async def collect() -> dict[str, int]:
    async with AsyncSessionLocal() as db:
        counts: dict[str, int] = {}
        # A 64-character hex string is the legacy unsalted SHA-256 shape; bcrypt
        # hashes start with $2a$/$2b$/$2y$ and PBKDF2 ones with pbkdf2_sha256$.
        counts["admin_users_total"] = await db.scalar(text("SELECT COUNT(*) FROM admin_users")) or 0
        counts["admin_users_legacy_sha256"] = await db.scalar(
            text(
                "SELECT COUNT(*) FROM admin_users "
                "WHERE LENGTH(password_hash) = 64 AND password_hash NOT LIKE '$%' AND password_hash NOT LIKE '%$%'"
            )
        ) or 0
        counts["media_assets_total"] = await db.scalar(text("SELECT COUNT(*) FROM media_assets")) or 0
        counts["media_assets_duplicate_content_sha256"] = await db.scalar(
            text(
                "SELECT COUNT(*) FROM (SELECT content_sha256 FROM media_assets "
                "WHERE content_sha256 IS NOT NULL GROUP BY content_sha256 HAVING COUNT(*) > 1) AS duplicates"
            )
        ) or 0
        for table_name in ("room_profiles", "user_profiles"):
            counts[f"{table_name}_null_avatar_status"] = await db.scalar(
                text(f"SELECT COUNT(*) FROM {table_name} WHERE avatar_status IS NULL")
            ) or 0
        return counts


if __name__ == "__main__":
    print(json.dumps(asyncio.run(collect()), ensure_ascii=False, indent=2, sort_keys=True))
