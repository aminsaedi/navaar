from __future__ import annotations

from unittest.mock import MagicMock

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


@pytest.fixture
def mock_sp_client() -> MagicMock:
    client = MagicMock()
    client.get_playlist_tracks = MagicMock(return_value=[])
    client.find_best_match = MagicMock(
        return_value={"id": "sp123", "name": "Hello", "artists": ["Adele"], "uri": "spotify:track:sp123"}
    )
    client.is_in_playlist = MagicMock(return_value=False)
    client.add_to_playlist = MagicMock(return_value=None)
    client.search_track = MagicMock(return_value=[
        {"id": "sp123", "name": "Hello", "artists": ["Adele"], "duration_ms": 300000, "uri": "spotify:track:sp123"}
    ])
    return client
