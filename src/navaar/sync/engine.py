from __future__ import annotations

import asyncio
import time
from typing import Callable

import structlog

from navaar.db.repository import SyncStateRepository, TrackRepository
from navaar.metrics import (
    ALL_DIRECTIONS,
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

logger = structlog.get_logger()


class SyncEngine:
    def __init__(
        self,
        sync_modules: dict[str, object],
        intervals: dict[str, int],
        track_repo: TrackRepository,
        sync_state: SyncStateRepository,
    ) -> None:
        self._track_repo = track_repo
        self._sync_state = sync_state
        self._shutdown = asyncio.Event()
        self._force_events: dict[str, asyncio.Event] = {
            d: asyncio.Event() for d in sync_modules
        }

        # Build cycle methods: direction -> callable
        self._cycle_methods: dict[str, Callable] = {}
        self._intervals: dict[str, int] = {}
        for direction, module in sync_modules.items():
            if hasattr(module, "process_pending"):
                self._cycle_methods[direction] = module.process_pending
            elif hasattr(module, "process_new_tracks"):
                self._cycle_methods[direction] = module.process_new_tracks
            else:
                raise ValueError(
                    f"Module for {direction} has neither process_pending nor process_new_tracks"
                )
            self._intervals[direction] = intervals.get(direction, 120)

    def request_shutdown(self) -> None:
        self._shutdown.set()

    def force_sync(self, direction: str) -> None:
        if direction in self._force_events:
            self._force_events[direction].set()

    async def run(self) -> None:
        logger.info("sync_engine_starting")
        await asyncio.gather(
            *(
                self._run_loop(d, self._intervals[d])
                for d in self._cycle_methods
            )
        )
        logger.info("sync_engine_stopped")

    async def _run_loop(self, direction: str, interval: int) -> None:
        force_event = self._force_events[direction]
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

        cycle_fn = self._cycle_methods[direction]
        processed = await cycle_fn()

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
        for direction in ALL_DIRECTIONS:
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
