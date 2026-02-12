from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from navaar.db.models import Base
from navaar.db.repository import SyncLogRepository, SyncStateRepository, TrackRepository


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory

    await engine.dispose()


@pytest.fixture
async def track_repo(session_factory: async_sessionmaker[AsyncSession]) -> TrackRepository:
    return TrackRepository(session_factory)


@pytest.fixture
async def sync_state_repo(session_factory: async_sessionmaker[AsyncSession]) -> SyncStateRepository:
    return SyncStateRepository(session_factory)


@pytest.fixture
async def sync_log_repo(session_factory: async_sessionmaker[AsyncSession]) -> SyncLogRepository:
    return SyncLogRepository(session_factory)
