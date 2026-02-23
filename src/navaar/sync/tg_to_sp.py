from __future__ import annotations

import time

import structlog

from navaar.db.repository import SyncLogRepository, TrackRepository
from navaar.metrics import (
    DUPLICATES_SKIPPED,
    IDENTIFICATION_TOTAL,
    SP_SEARCH_DURATION,
    SP_SEARCH_TOTAL,
    SYNC_ERRORS,
    TRACK_SYNC_DURATION,
    TRACKS_SYNCED,
)
from navaar.spotify.client import SpotifyClient
from navaar.sync.identifier import identify_track
from navaar.telegram.client import TelegramClient

logger = structlog.get_logger()


class TgToSpSync:
    def __init__(
        self,
        track_repo: TrackRepository,
        sync_log: SyncLogRepository,
        tg_client: TelegramClient,
        sp_client: SpotifyClient,
    ) -> None:
        self._tracks = track_repo
        self._log = sync_log
        self._tg = tg_client
        self._sp = sp_client

    async def process_pending(self) -> int:
        pending = await self._tracks.get_pending_tracks("tg_to_sp")
        if not pending:
            return 0

        logger.info("tg_to_sp_processing", count=len(pending))
        processed = 0
        playlist_tracks = self._sp.get_playlist_tracks()

        for track in pending:
            try:
                await self._process_track(track, playlist_tracks)
                processed += 1
            except Exception:
                logger.error(
                    "tg_to_sp_track_error",
                    track_id=track.id,
                    exc_info=True,
                )
                await self._tracks.mark_failed(track.id, "unexpected_error")
                await self._log.log(
                    "sync_failed",
                    track_id=track.id,
                    direction="tg_to_sp",
                    details={"reason": "unexpected_error"},
                )
                SYNC_ERRORS.labels(direction="tg_to_sp", error_type="unexpected").inc()

        return processed

    async def _process_track(self, track, playlist_tracks: list[dict]) -> None:
        start = time.monotonic()
        await self._tracks.update_track(track.id, status="identifying")

        # Step 1: Download file from Telegram and identify
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

        # Reload track with updated metadata
        track = await self._tracks.get_track(track.id)

        # Step 2: Search Spotify
        search_start = time.monotonic()
        match = self._sp.find_best_match(track.artist, track.title)
        SP_SEARCH_DURATION.observe(time.monotonic() - search_start)

        if not match:
            await self._tracks.mark_failed(track.id, "no_sp_match")
            await self._log.log(
                "no_sp_match",
                track_id=track.id,
                direction="tg_to_sp",
                details={"artist": track.artist, "title": track.title},
            )
            SP_SEARCH_TOTAL.labels(result="not_found").inc()
            SYNC_ERRORS.labels(direction="tg_to_sp", error_type="no_sp_match").inc()
            return

        SP_SEARCH_TOTAL.labels(result="found").inc()
        sp_track_id = match["id"]

        # Step 3: Check for duplicates
        if self._sp.is_in_playlist(sp_track_id, playlist_tracks):
            await self._tracks.mark_duplicate(track.id)
            await self._tracks.update_track(track.id, sp_track_id=sp_track_id)
            await self._log.log(
                "duplicate_skipped",
                track_id=track.id,
                direction="tg_to_sp",
                details={"sp_track_id": sp_track_id},
            )
            DUPLICATES_SKIPPED.labels(direction="tg_to_sp").inc()
            logger.info("duplicate_skipped", track_id=track.id, sp_track_id=sp_track_id)
            return

        # Step 4: Add to playlist
        await self._tracks.update_track(track.id, status="syncing")
        self._sp.add_to_playlist(sp_track_id)

        await self._tracks.mark_synced(track.id, sp_track_id=sp_track_id)
        await self._log.log(
            "track_synced",
            track_id=track.id,
            direction="tg_to_sp",
            details={"sp_track_id": sp_track_id, "name": match.get("name")},
        )

        TRACKS_SYNCED.labels(direction="tg_to_sp").inc()
        TRACK_SYNC_DURATION.labels(direction="tg_to_sp").observe(time.monotonic() - start)
        logger.info(
            "tg_to_sp_synced",
            track_id=track.id,
            sp_track_id=sp_track_id,
            name=match.get("name"),
        )
