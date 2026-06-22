from __future__ import annotations

import asyncio
import time

import structlog

from navaar.db.repository import SyncLogRepository, TrackRepository
from navaar.metrics import (
    DUPLICATES_SKIPPED,
    IDENTIFICATION_TOTAL,
    SYNC_ERRORS,
    TRACK_SYNC_DURATION,
    TRACKS_SYNCED,
)
from navaar.sync._targets import TargetAdapter
from navaar.sync.identifier import identify_track
from navaar.telegram.client import TelegramClient

logger = structlog.get_logger()


class BasePushSync:
    """Shared skeleton for the four push directions (tg→yt, tg→sp, yt→sp, sp→yt).

    They differ only in: the target service (captured by a TargetAdapter) and
    whether the source is a Telegram audio file that must first be downloaded and
    identified. Subclasses set ``direction``, ``target``, ``identify_from_telegram``
    and wire the target client.
    """

    direction: str
    target: TargetAdapter
    identify_from_telegram: bool = False

    def __init__(
        self,
        track_repo: TrackRepository,
        sync_log: SyncLogRepository,
        target_client: object,
        tg_client: TelegramClient | None = None,
    ) -> None:
        self._tracks = track_repo
        self._log = sync_log
        self._target = target_client
        self._tg = tg_client

    async def process_pending(self) -> int:
        pending = await self._tracks.get_pending_tracks(self.direction)
        if not pending:
            return 0

        logger.info(f"{self.direction}_processing", count=len(pending))
        processed = 0
        # Blocking client call off the event loop. A permanent auth failure here
        # propagates to the engine (the systemic-failure path) by design.
        playlist_tracks = await asyncio.to_thread(self._target.get_playlist_tracks)

        for track in pending:
            try:
                await self._process_track(track, playlist_tracks)
                processed += 1
            except Exception:
                logger.error(f"{self.direction}_track_error", track_id=track.id, exc_info=True)
                await self._tracks.mark_failed(track.id, "unexpected_error")
                await self._log.log(
                    "sync_failed",
                    track_id=track.id,
                    direction=self.direction,
                    details={"reason": "unexpected_error"},
                )
                SYNC_ERRORS.labels(direction=self.direction, error_type="unexpected").inc()

        return processed

    async def _process_track(self, track, playlist_tracks: list[dict]) -> None:
        start = time.monotonic()
        t = self.target

        if self.identify_from_telegram:
            track = await self._identify_from_telegram(track)
        else:
            await self._tracks.update_track(track.id, status="searching")
            track = await self._tracks.get_track(track.id)

        search_start = time.monotonic()
        match = await asyncio.to_thread(self._target.find_best_match, track.artist, track.title)
        t.search_duration.observe(time.monotonic() - search_start)

        if not match:
            await self._tracks.mark_failed(track.id, t.no_match_reason)
            await self._log.log(
                t.no_match_reason,
                track_id=track.id,
                direction=self.direction,
                details={"artist": track.artist, "title": track.title},
            )
            t.search_total.labels(result="not_found").inc()
            SYNC_ERRORS.labels(direction=self.direction, error_type=t.no_match_reason).inc()
            return

        t.search_total.labels(result="found").inc()
        ext_id = match[t.match_id_key]

        if self._target.is_in_playlist(ext_id, playlist_tracks):
            await self._tracks.mark_duplicate(track.id)
            await self._tracks.update_track(track.id, **{t.db_field: ext_id})
            await self._log.log(
                "duplicate_skipped",
                track_id=track.id,
                direction=self.direction,
                details={t.db_field: ext_id},
            )
            DUPLICATES_SKIPPED.labels(direction=self.direction).inc()
            logger.info("duplicate_skipped", track_id=track.id, **{t.db_field: ext_id})
            return

        await self._tracks.update_track(track.id, status="syncing")
        await asyncio.to_thread(self._target.add_to_playlist, ext_id)

        await self._tracks.mark_synced(track.id, **{t.db_field: ext_id})
        await self._log.log(
            "track_synced",
            track_id=track.id,
            direction=self.direction,
            details={t.db_field: ext_id, "name": match.get(t.match_name_key)},
        )
        TRACKS_SYNCED.labels(direction=self.direction).inc()
        TRACK_SYNC_DURATION.labels(direction=self.direction).observe(time.monotonic() - start)
        logger.info(
            f"{self.direction}_synced",
            track_id=track.id,
            **{t.db_field: ext_id, t.match_name_key: match.get(t.match_name_key)},
        )

    async def _identify_from_telegram(self, track):
        """Download the Telegram audio and run the identification pipeline, then
        return the reloaded track with updated metadata."""
        await self._tracks.update_track(track.id, status="identifying")
        local_path = None
        try:
            if track.tg_file_id:
                local_path = await self._tg.download_file(track.tg_file_id)

            info = identify_track(
                file_path=local_path,
                tg_performer=track.artist,
                tg_title=track.title,
                file_name=local_path,
            )

            if info:
                await self._tracks.update_track(
                    track.id,
                    artist=info.artist or track.artist,
                    title=info.title,
                    identification_method=info.method,
                    status="searching",
                )
                IDENTIFICATION_TOTAL.labels(method=info.method).inc()
            else:
                await self._tracks.update_track(track.id, status="searching")
        finally:
            if local_path:
                self._tg.cleanup(local_path)

        return await self._tracks.get_track(track.id)
