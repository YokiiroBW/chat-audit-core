import asyncio

from httpx import ASGITransport, AsyncClient, TimeoutException
import pytest

from app.database import create_all_tables, create_async_engine_and_sessionmaker, get_db_session
from app.main import app
from app.services.message_service import MessageService
from app.services.query_service import QueryService


def _message_payload(index: int, raw_message: str | None = None) -> dict:
    return {
        "message_id": f"msg-{index}",
        "room_id": "group-coverage",
        "message_type": "group",
        "sender_id": f"user-{index % 10}",
        "nickname": f"User {index % 10}",
        "raw_message": raw_message if raw_message is not None else f"coverage message {index}",
        "timestamp": 1783300000 + index,
    }


@pytest.mark.asyncio
async def test_concurrent_message_ingestion_returns_unique_hashes(tmp_path):
    database_path = tmp_path / "concurrent.sqlite3"
    engine, sessionmaker = create_async_engine_and_sessionmaker(f"sqlite+aiosqlite:///{database_path.as_posix()}")
    await create_all_tables(engine)
    semaphore = asyncio.Semaphore(10)

    async def send_message(index: int) -> str | None:
        async with semaphore:
            async with sessionmaker() as session:
                return await MessageService.process_incoming_message(
                    session,
                    robot_id="robot-coverage",
                    platform="qq",
                    msg_data=_message_payload(index),
                )

    try:
        hashes = await asyncio.gather(*(send_message(index) for index in range(100)))
        async with sessionmaker() as session:
            messages = await MessageService.list_messages(session)
            robot_messages = await MessageService.list_robot_messages(session)
    finally:
        await engine.dispose()

    assert len(set(hashes)) == 100
    assert len(messages) == 100
    assert len(robot_messages) == 100


@pytest.mark.asyncio
async def test_message_ingestion_preserves_empty_large_and_special_text(db_session):
    payloads = [
        _message_payload(1, ""),
        _message_payload(2, "A" * 100_000),
        _message_payload(3, "<script>alert('x')</script>&\"'\n\t"),
    ]

    hashes = [
        await MessageService.process_incoming_message(
            db_session,
            robot_id="robot-coverage",
            platform="qq",
            msg_data=payload,
        )
        for payload in payloads
    ]
    messages = await MessageService.list_messages(db_session)

    assert all(hashes)
    assert [message.raw_message for message in messages] == [payload["raw_message"] for payload in payloads]
    assert messages[0].local_message == ""
    assert len(messages[1].raw_message) == 100_000


@pytest.mark.asyncio
async def test_media_download_timeout_keeps_original_message(db_session, tmp_path):
    class TimeoutClient:
        async def get(self, _url: str):
            raise TimeoutException("media request timed out")

    raw_message = "[CQ:image,file=timeout.jpg,url=http://media.local/timeout.jpg]"

    msg_hash = await MessageService.process_incoming_message(
        db_session,
        robot_id="robot-coverage",
        platform="qq",
        msg_data=_message_payload(4, raw_message),
        media_http_client=TimeoutClient(),
        media_storage_root=tmp_path,
        media_public_prefix="/static/storage",
    )
    messages = await MessageService.list_messages(db_session)
    assets = await MessageService.list_media_assets(db_session)

    assert msg_hash
    assert messages[0].raw_message == raw_message
    assert messages[0].local_message == raw_message
    assert assets == []


@pytest.mark.asyncio
async def test_message_api_end_to_end_ingest_room_and_message_query(db_session):
    async def override_db_session():
        yield db_session

    app.dependency_overrides[get_db_session] = override_db_session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            ingest_response = await client.post(
                "/api/messages",
                json={
                    "robot_id": "robot-e2e",
                    "platform": "qq",
                    "room_id": "group-e2e",
                    "message_type": "group",
                    "sender_id": "user-e2e",
                    "nickname": "E2E User",
                    "raw_message": "end to end message",
                    "timestamp": 1783300500,
                    "message_id": "e2e-message-1",
                },
            )
            rooms_response = await client.get("/api/rooms", params={"robot_id": "robot-e2e"})
            messages_response = await client.get(
                "/api/messages",
                params={"robot_id": "robot-e2e", "room_id": "group-e2e"},
            )
    finally:
        app.dependency_overrides.clear()

    assert ingest_response.status_code == 201
    assert ingest_response.json()["skipped"] is False
    assert rooms_response.status_code == 200
    assert rooms_response.json()[0]["room_id"] == "group-e2e"
    assert messages_response.status_code == 200
    assert messages_response.json()[0]["external_message_id"] == "e2e-message-1"
    assert messages_response.json()[0]["raw_message"] == "end to end message"


@pytest.mark.asyncio
async def test_query_service_can_read_after_concurrent_ingestion(tmp_path):
    database_path = tmp_path / "query-after-concurrent.sqlite3"
    engine, sessionmaker = create_async_engine_and_sessionmaker(f"sqlite+aiosqlite:///{database_path.as_posix()}")
    await create_all_tables(engine)

    async def send_message(index: int) -> str | None:
        async with sessionmaker() as session:
            return await MessageService.process_incoming_message(
                session,
                robot_id="robot-query",
                platform="qq",
                msg_data=_message_payload(index),
            )

    try:
        await asyncio.gather(*(send_message(index) for index in range(10)))
        async with sessionmaker() as session:
            rooms = await QueryService.list_rooms(session, robot_id="robot-query")
            messages = await QueryService.list_messages(session, robot_id="robot-query", room_id="group-coverage")
    finally:
        await engine.dispose()

    assert rooms == [{"room_id": "group-coverage", "last_timestamp": 1783300009, "message_type": "group", "display_name": None, "avatar_path": None}]
    assert [message.external_message_id for message in messages] == [f"msg-{index}" for index in range(10)]
