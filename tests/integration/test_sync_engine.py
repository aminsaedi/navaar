from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from navaar.db.repository import SyncStateRepository, TrackRepository
from navaar.sync.engine import SyncEngine


@pytest.fixture
def mock_tg_to_yt() -> MagicMock:
    m = MagicMock(spec=["process_pending"])
    m.process_pending = AsyncMock(return_value=0)
    return m


@pytest.fixture
def mock_yt_to_tg() -> MagicMock:
    m = MagicMock(spec=["process_new_tracks"])
    m.process_new_tracks = AsyncMock(return_value=0)
    return m


@pytest.mark.asyncio
async def test_engine_starts_and_stops(
    track_repo: TrackRepository,
    sync_state_repo: SyncStateRepository,
    mock_tg_to_yt: MagicMock,
    mock_yt_to_tg: MagicMock,
) -> None:
    engine = SyncEngine(
        sync_modules={"tg_to_yt": mock_tg_to_yt, "yt_to_tg": mock_yt_to_tg},
        intervals={"tg_to_yt": 1, "yt_to_tg": 1},
        track_repo=track_repo,
        sync_state=sync_state_repo,
    )

    # Run engine for a short time then stop
    async def stop_after_delay() -> None:
        await asyncio.sleep(0.5)
        engine.request_shutdown()

    await asyncio.gather(engine.run(), stop_after_delay())

    # Both loops should have run at least once
    mock_tg_to_yt.process_pending.assert_called()
    mock_yt_to_tg.process_new_tracks.assert_called()


@pytest.mark.asyncio
async def test_force_sync(
    track_repo: TrackRepository,
    sync_state_repo: SyncStateRepository,
    mock_tg_to_yt: MagicMock,
    mock_yt_to_tg: MagicMock,
) -> None:
    engine = SyncEngine(
        sync_modules={"tg_to_yt": mock_tg_to_yt, "yt_to_tg": mock_yt_to_tg},
        intervals={"tg_to_yt": 60, "yt_to_tg": 60},
        track_repo=track_repo,
        sync_state=sync_state_repo,
    )

    async def force_and_stop() -> None:
        await asyncio.sleep(0.2)
        engine.force_sync("tg_to_yt")
        await asyncio.sleep(0.5)
        engine.request_shutdown()

    await asyncio.gather(engine.run(), force_and_stop())
    # Should have been called at least twice (initial + forced)
    assert mock_tg_to_yt.process_pending.call_count >= 2
