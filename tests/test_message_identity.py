import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Base, ImportBatch, ImportSource, Message, MessageSourceRecord, RobotMessage
from app.schemas import ImportMessageItemRequest
from app.services.import_service import ImportService
from app.services.message_service import MessageService


@pytest_asyncio.fixture
async def archive(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'archive.sqlite'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        source = ImportSource(
            id="source-one", source_type="qqnt_local_db", platform="qq",
            account_id="bot-one", device_id="synthetic-device",
        )
        batch = ImportBatch(id="batch-one", source_id=source.id, mode="initial")
        db.add_all([source, batch])
        await db.commit()
        yield db, source, batch
    await engine.dispose()


def realtime(message_id, message_type="group"):
    return dict(
        room_id="room-one", sender_id="sender-one", message_type=message_type,
        raw_message="same short text", timestamp=1_780_000_000, message_id=message_id,
    )


def imported(message_id, *, platform_id=None, key=None, message_type="group"):
    return ImportMessageItemRequest(
        message=dict(realtime(message_id, message_type), robot_id="bot-one", platform="qq"),
        source_record=dict(
            source_table="messages", source_key=key or message_id,
            platform_message_id=platform_id,
        ),
    )


async def import_item(archive, item):
    db, source, batch = archive
    result = await ImportService._upsert_message_item(db, source=source, batch=batch, item=item)
    await db.commit()
    return result[0]


async def capture(archive, message_id, message_type="group"):
    return await MessageService.process_incoming_message(
        archive[0], "bot-one", "qq", realtime(message_id, message_type),
    )


async def count(archive, model):
    return await archive[0].scalar(select(func.count()).select_from(model))


@pytest.mark.parametrize("first", ["import", "realtime"])
async def test_distinct_ids_at_same_second_are_preserved_in_both_arrival_orders(archive, first):
    actions = [lambda: import_item(archive, imported("import-id")), lambda: capture(archive, "live-id")]
    if first == "realtime":
        actions.reverse()
    hashes = [await action() for action in actions]
    assert hashes[0] != hashes[1]
    assert await count(archive, Message) == 2
    assert await count(archive, RobotMessage) == 2
    assert await capture(archive, "live-id") in hashes
    assert await import_item(archive, imported("import-id")) in hashes
    assert await count(archive, Message) == 2


@pytest.mark.parametrize("first", ["import", "realtime"])
@pytest.mark.parametrize("shared_platform_id", [False, True])
async def test_same_message_replay_converges_via_id_or_recorded_platform_alias(
    archive, first, shared_platform_id,
):
    item = imported("import-id" if shared_platform_id else "shared-id",
                    platform_id="shared-id" if shared_platform_id else None)
    actions = [lambda: import_item(archive, item), lambda: capture(archive, "shared-id")]
    if first == "realtime":
        actions.reverse()
    assert await actions[0]() == await actions[1]()
    assert await count(archive, Message) == 1
    assert await count(archive, MessageSourceRecord) == 1
    assert await count(archive, RobotMessage) == 1


async def test_source_row_replay_does_not_depend_on_regenerated_import_id(archive):
    first = await import_item(archive, imported("import-id", key="stable-row"))
    assert await import_item(archive, imported("refreshed-import-id", key="stable-row")) == first
    assert await count(archive, Message) == 1


async def test_two_import_sources_do_not_merge_on_text_and_timestamp(archive):
    first = await import_item(archive, imported("first-source-id"))
    db, source, batch = archive
    second_source = ImportSource(
        id="source-two", source_type="qqnt_local_db", platform="qq",
        account_id="bot-one", device_id="other-synthetic-device",
    )
    db.add(second_source)
    await db.commit()
    second_archive = db, second_source, batch
    assert await import_item(second_archive, imported("second-source-id")) != first
    assert await count(archive, Message) == 2


async def test_unidentified_deliveries_are_preserved(archive):
    assert await capture(archive, None) != await capture(archive, None)
    assert await count(archive, Message) == 2


async def test_platform_alias_cannot_merge_private_and_group_messages(archive):
    first = await import_item(archive, imported("import-id", platform_id="shared-id"))
    assert await capture(archive, "shared-id", "private") != first
    assert await count(archive, Message) == 2
