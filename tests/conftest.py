import pytest_asyncio

from app.database import create_async_engine_and_sessionmaker, create_all_tables


@pytest_asyncio.fixture
async def db_session():
    engine, sessionmaker = create_async_engine_and_sessionmaker("sqlite+aiosqlite:///:memory:")
    await create_all_tables(engine)
    async with sessionmaker() as session:
        yield session
    await engine.dispose()
