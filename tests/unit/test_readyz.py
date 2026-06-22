from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from navaar.api.server import create_app
from navaar.db.repository import SyncStateRepository, TrackRepository


@pytest.mark.asyncio
async def test_readyz_ok_when_recent(
    track_repo: TrackRepository, sync_state_repo: SyncStateRepository
) -> None:
    # A direction that synced just now is healthy.
    await sync_state_repo.set("last_tg_to_yt_sync", str(time.time()))
    app = create_app(
        track_repo=track_repo,
        sync_state=sync_state_repo,
        intervals={"tg_to_yt": 60},
        stale_multiplier=5,
        start_time=time.time(),
    )
    with TestClient(app) as client:
        resp = client.get("/readyz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_readyz_degraded_when_stale(
    track_repo: TrackRepository, sync_state_repo: SyncStateRepository
) -> None:
    # A direction whose last sync is far older than interval*multiplier is stale,
    # so the pod reports NotReady (503) — the crash-loop becomes visible.
    await sync_state_repo.set("last_tg_to_yt_sync", str(time.time() - 10_000))
    app = create_app(
        track_repo=track_repo,
        sync_state=sync_state_repo,
        intervals={"tg_to_yt": 60},
        stale_multiplier=5,  # threshold 300s
        start_time=time.time() - 10_000,
    )
    with TestClient(app) as client:
        resp = client.get("/readyz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert "tg_to_yt" in body["stale"]


@pytest.mark.asyncio
async def test_readyz_grace_at_startup(
    track_repo: TrackRepository, sync_state_repo: SyncStateRepository
) -> None:
    # Fresh boot, no timestamps yet: within the grace window the pod is Ready.
    app = create_app(
        track_repo=track_repo,
        sync_state=sync_state_repo,
        intervals={"tg_to_yt": 60},
        stale_multiplier=5,
        start_time=time.time(),
    )
    with TestClient(app) as client:
        resp = client.get("/readyz")
    assert resp.status_code == 200
