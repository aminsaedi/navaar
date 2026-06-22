from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from navaar.db.repository import SyncStateRepository, TrackRepository
from navaar.metrics import AUTH_ERRORS, DIRECTION_HEALTH, SYNC_CYCLE_CRASHES, SYNC_ERRORS
from navaar.sync.engine import SyncEngine


def _counter_value(metric, **labels) -> float:
    return metric.labels(**labels)._value.get()


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


@pytest.mark.asyncio
async def test_cycle_crash_does_not_kill_loop_or_siblings(
    track_repo: TrackRepository,
    sync_state_repo: SyncStateRepository,
) -> None:
    # A cycle that throws every iteration must be caught, metered, and must not
    # kill its own loop or stop a sibling direction. This is the exact production
    # incident (a direction crashing on every cycle for an extended period).
    crasher = MagicMock(spec=["process_pending"])
    crasher.process_pending = AsyncMock(side_effect=RuntimeError("boom"))
    healthy = MagicMock(spec=["process_new_tracks"])
    healthy.process_new_tracks = AsyncMock(return_value=0)
    alerts = AsyncMock()

    engine = SyncEngine(
        sync_modules={"tg_to_yt": crasher, "yt_to_tg": healthy},
        intervals={"tg_to_yt": 0, "yt_to_tg": 0},
        track_repo=track_repo,
        sync_state=sync_state_repo,
        alert_notifier=alerts,
    )

    before = _counter_value(SYNC_CYCLE_CRASHES, direction="tg_to_yt")

    async def stop() -> None:
        await asyncio.sleep(0.3)
        engine.request_shutdown()

    await asyncio.gather(engine.run(), stop())

    assert crasher.process_pending.call_count >= 2  # kept looping despite raising
    assert healthy.process_new_tracks.call_count >= 2  # sibling unaffected
    assert _counter_value(SYNC_CYCLE_CRASHES, direction="tg_to_yt") > before
    alerts.record_crash.assert_awaited()  # systemic failure was surfaced


@pytest.mark.asyncio
async def test_auth_error_is_classified_and_metered(
    track_repo: TrackRepository,
    sync_state_repo: SyncStateRepository,
) -> None:
    # A revoked-token style 401 to googleapis should be counted as an auth error
    # (distinct from a generic crash) and attributed to the right service.
    req = httpx.Request("POST", "https://oauth2.googleapis.com/token")
    resp = httpx.Response(401, request=req)
    auth_exc = httpx.HTTPStatusError("401", request=req, response=resp)

    crasher = MagicMock(spec=["process_pending"])
    crasher.process_pending = AsyncMock(side_effect=auth_exc)

    engine = SyncEngine(
        sync_modules={"yt_to_sp": crasher},
        intervals={"yt_to_sp": 0},
        track_repo=track_repo,
        sync_state=sync_state_repo,
        circuit_open_after=2,
    )

    before_auth = _counter_value(AUTH_ERRORS, service="yt")
    before_err = _counter_value(SYNC_ERRORS, direction="yt_to_sp", error_type="auth_error")

    async def stop() -> None:
        await asyncio.sleep(0.2)
        engine.request_shutdown()

    await asyncio.gather(engine.run(), stop())

    assert _counter_value(AUTH_ERRORS, service="yt") > before_auth
    assert _counter_value(SYNC_ERRORS, direction="yt_to_sp", error_type="auth_error") > before_err
    # Repeated crashes open the circuit -> direction reports unhealthy.
    assert DIRECTION_HEALTH.labels(direction="yt_to_sp")._value.get() == 0
