from __future__ import annotations

import argparse
import asyncio
import json
import resource
import tempfile
from pathlib import Path

from sqlalchemy import insert

from app.backup.exporter import create_full_backup_archive
from app.database import create_all_tables, create_async_engine_and_sessionmaker
from app.models import MediaAsset, Message, RobotMessage


async def probe(media_bytes: int, messages: int) -> dict[str, float | int]:
    with tempfile.TemporaryDirectory(prefix="cacb-memory-probe-") as raw:
        root = Path(raw)
        storage = root / "storage"
        backup_root = root / "backups"
        storage.mkdir()
        media_path = storage / "large-media.bin"
        with media_path.open("wb") as file:
            file.truncate(media_bytes)
        database_url = f"sqlite+aiosqlite:///{root / 'source.sqlite3'}"
        engine, sessionmaker = create_async_engine_and_sessionmaker(database_url)
        await create_all_tables(engine)
        async with sessionmaker() as db:
            for offset in range(0, messages, 500):
                rows = [
                    {
                        "msg_hash": f"message-{index:08d}",
                        "platform": "qq",
                        "room_id": "room-memory",
                        "message_type": "group",
                        "sender_id": f"user-{index % 1000}",
                        "raw_message": "x" * 2048,
                        "local_message": "x" * 2048,
                        "timestamp": index,
                    }
                    for index in range(offset, min(messages, offset + 500))
                ]
                await db.execute(insert(Message), rows)
                await db.execute(
                    insert(RobotMessage),
                    [{"robot_id": "robot-memory", "msg_hash": row["msg_hash"]} for row in rows],
                )
            await db.execute(
                insert(MediaAsset),
                [
                    {
                        "file_hash": "large-media",
                        "file_type": "file",
                        "file_size": media_bytes,
                        "local_path": "/static/storage/large-media.bin",
                    }
                ],
            )
            await db.commit()
        result = await create_full_backup_archive(
            database_url=database_url,
            storage_root=storage,
            backup_root=backup_root,
            public_storage_prefix="/static/storage",
            signing_key="memory-probe-key",
            system_id="memory-probe",
            backup_type="manual",
            created_by="memory_probe",
            min_free_bytes=0,
        )
        peak_rss_mib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
        output = {
            "peak_rss_mib": round(peak_rss_mib, 3),
            "media_bytes": media_bytes,
            "messages": messages,
            "archive_bytes": result.path.stat().st_size,
        }
        await engine.dispose()
        return output


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--media-bytes", type=int, required=True)
    parser.add_argument("--messages", type=int, required=True)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(probe(args.media_bytes, args.messages)), sort_keys=True))


if __name__ == "__main__":
    main()
