from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

import structlog

from navaar.auth_errors import classify_auth_service
from navaar.db.repository import SyncStateRepository, TrackRepository
from navaar.metrics import (
    ALL_DIRECTIONS,
    AUTH_ERRORS,
    DIRECTION_HEALTH,
    LAST_SYNC_DURATION,
    LAST_SYNC_PROCESSED,
    LAST_SYNC_TIMESTAMP,
    SUCCESS_RATE,
    SYNC_CYCLE_CRASHES,
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
        alert_notifier: object | None = None,
        backoff_max_seconds: int = 1800,
        circuit_open_after: int = 5,
    ) -> None:
        self._track_repo = track_repo
        self._sync_state = sync_state
        self._alerts = alert_notifier
        self._backoff_max = backoff_max_seconds
        self._circuit_open_after = circuit_open_after
        self._shutdown = asyncio.Event()
        self._force_events: dict[str, asyncio.Event] = {
            d: asyncio.Event() for d in sync_modules
        }
        self._failures: dict[str, int] = dict.fromkeys(sync_modules, 0)

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
                await self._on_success(direction)
            except Exception as exc:
                await self._on_crash(direction, exc)

            # Back off the next sleep while a direction is failing, so a permanent
            # failure (e.g. revoked token) can't hammer the API every interval.
            sleep_for = self._next_interval(direction, interval)

            # Wait for interval or force event or shutdown
            try:
                done, pending = await asyncio.wait(
                    [
                        asyncio.create_task(self._shutdown.wait()),
                        asyncio.create_task(force_event.wait()),
                        asyncio.create_task(asyncio.sleep(sleep_for)),
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

    async def _on_success(self, direction: str) -> None:
        """Reset failure state and clear any open alert after a clean cycle."""
        if self._failures.get(direction):
            self._failures[direction] = 0
            DIRECTION_HEALTH.labels(direction=direction).set(1)
        if self._alerts is not None:
            try:
                await self._alerts.record_success(direction)
            except Exception:
                logger.warning("alert_dispatch_failed", direction=direction, exc_info=True)

    async def _on_crash(self, direction: str, exc: Exception) -> None:
        """Log, classify (auth vs generic), meter, and alert on a crashed cycle."""
        logger.error("sync_cycle_crashed", direction=direction, exc_info=True)
        SYNC_CYCLE_CRASHES.labels(direction=direction).inc()

        service = classify_auth_service(exc)
        if service is not None:
            AUTH_ERRORS.labels(service=service).inc()
            SYNC_ERRORS.labels(direction=direction, error_type="auth_error").inc()
        else:
            SYNC_ERRORS.labels(direction=direction, error_type="cycle_crash").inc()

        self._failures[direction] = self._failures.get(direction, 0) + 1
        if self._failures[direction] >= self._circuit_open_after:
            DIRECTION_HEALTH.labels(direction=direction).set(0)

        if self._alerts is not None:
            try:
                await self._alerts.record_crash(direction, exc)
            except Exception:
                logger.warning("alert_dispatch_failed", direction=direction, exc_info=True)

    def _next_interval(self, direction: str, interval: int) -> float:
        """Exponential backoff while a direction is failing, capped; the normal
        interval once it's healthy."""
        fails = self._failures.get(direction, 0)
        if not fails:
            return interval
        return min(interval * (2 ** min(fails, 5)), self._backoff_max)

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
