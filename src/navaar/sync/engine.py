from __future__ import annotations

import asyncio
import time

import structlog

from navaar.db.repository import SyncStateRepository
from navaar.metrics import (
    LAST_SYNC_DURATION,
    LAST_SYNC_PROCESSED,
    LAST_SYNC_TIMESTAMP,
    SUCCESS_RATE,
    SYNC_CYCLE_DURATION,
    SYNC_CYCLES,
    SYNC_ERRORS,
    TRACKS_DUPLICATE_GAUGE,
    TRACKS_FAILED_GAUGE,
    TRACKS_PENDING_GAUGE,
    TRACKS_SYNCED_GAUGE,
    TRACKS_TOTAL_GAUGE,
)
from navaar.db.repository import TrackRepository
from navaar.sync.tg_to_yt import TgToYtSync
from navaar.sync.yt_to_tg import YtToTgSync

logger = structlog.get_logger()


class SyncEngine:
    def __init__(
        self,
        tg_to_yt: TgToYtSync,
        yt_to_tg: YtToTgSync,
        track_repo: TrackRepository,
        sync_state: SyncStateRepository,
        tg_to_yt_interval: int = 60,
        yt_to_tg_interval: int = 120,
    ) -> None:
        self._tg_to_yt = tg_to_yt
        self._yt_to_tg = yt_to_tg
        self._track_repo = track_repo
        self._sync_state = sync_state
        self._tg_to_yt_interval = tg_to_yt_interval
        self._yt_to_tg_interval = yt_to_tg_interval
        self._shutdown = asyncio.Event()
        self._force_tg_to_yt = asyncio.Event()
        self._force_yt_to_tg = asyncio.Event()

    def request_shutdown(self) -> None:
        self._shutdown.set()

    def force_sync(self, direction: str) -> None:
        if direction == "tg_to_yt":
            self._force_tg_to_yt.set()
        elif direction == "yt_to_tg":
            self._force_yt_to_tg.set()

    async def run(self) -> None:
        logger.info("sync_engine_starting")
        await asyncio.gather(
            self._run_loop("tg_to_yt", self._tg_to_yt_interval),
            self._run_loop("yt_to_tg", self._yt_to_tg_interval),
        )
        logger.info("sync_engine_stopped")

    async def _run_loop(self, direction: str, interval: int) -> None:
        force_event = self._force_tg_to_yt if direction == "tg_to_yt" else self._force_yt_to_tg
        logger.info("sync_loop_started", direction=direction, interval=interval)

        while not self._shutdown.is_set():
            try:
                await self._run_cycle(direction)
            except Exception:
                logger.error("sync_cycle_crashed", direction=direction, exc_info=True)
                SYNC_ERRORS.labels(direction=direction, error_type="cycle_crash").inc()

            # Wait for interval or force event or shutdown
            try:
                done, pending = await asyncio.wait(
                    [
                        asyncio.create_task(self._shutdown.wait()),
                        asyncio.create_task(force_event.wait()),
                        asyncio.create_task(asyncio.sleep(interval)),
                    ],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
            except asyncio.CancelledError:
                break

            if force_event.is_set():
                force_event.clear()
                logger.info("forced_sync", direction=direction)

        logger.info("sync_loop_stopped", direction=direction)

    async def _run_cycle(self, direction: str) -> None:
        start = time.monotonic()
        SYNC_CYCLES.labels(direction=direction).inc()
        logger.debug("sync_cycle_start", direction=direction)

        if direction == "tg_to_yt":
            processed = await self._tg_to_yt.process_pending()
        else:
            processed = await self._yt_to_tg.process_new_tracks()

        elapsed = time.monotonic() - start
        SYNC_CYCLE_DURATION.labels(direction=direction).observe(elapsed)
        LAST_SYNC_TIMESTAMP.labels(direction=direction).set(time.time())
        LAST_SYNC_DURATION.labels(direction=direction).set(round(elapsed, 3))
        LAST_SYNC_PROCESSED.labels(direction=direction).set(processed)

        # Update gauge metrics from DB
        await self._update_gauges()

        # Store last sync time
        await self._sync_state.set(f"last_{direction}_sync", str(time.time()))

        logger.info(
            "sync_cycle_complete",
            direction=direction,
            processed=processed,
            elapsed=round(elapsed, 2),
        )

    async def _update_gauges(self) -> None:
        counts = await self._track_repo.get_counts()
        total = 0
        total_synced = 0
        for direction in ("tg_to_yt", "yt_to_tg"):
            statuses = counts.get(direction, {})
            pending = statuses.get("pending", 0) + statuses.get("retry_scheduled", 0)
            failed = statuses.get("failed", 0)
            synced = statuses.get("synced", 0)
            dupes = statuses.get("duplicate", 0)
            dir_total = sum(statuses.values())

            TRACKS_PENDING_GAUGE.labels(direction=direction).set(pending)
            TRACKS_FAILED_GAUGE.labels(direction=direction).set(failed)
            TRACKS_SYNCED_GAUGE.labels(direction=direction).set(synced)
            TRACKS_DUPLICATE_GAUGE.labels(direction=direction).set(dupes)

            total += dir_total
            total_synced += synced

        TRACKS_TOTAL_GAUGE.set(total)
        SUCCESS_RATE.set(round(total_synced / total * 100, 1) if total > 0 else 0.0)
