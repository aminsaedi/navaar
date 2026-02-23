from __future__ import annotations

import time

import structlog

from navaar.db.repository import SyncLogRepository, TrackRepository
from navaar.metrics import (
    DUPLICATES_SKIPPED,
    SP_SEARCH_DURATION,
    SP_SEARCH_TOTAL,
    SYNC_ERRORS,
    TRACK_SYNC_DURATION,
    TRACKS_SYNCED,
)
from navaar.spotify.client import SpotifyClient
from navaar.ytmusic.client import YTMusicClient

logger = structlog.get_logger()


class YtToSpSync:
    def __init__(
        self,
        track_repo: TrackRepository,
        sync_log: SyncLogRepository,
        yt_client: YTMusicClient,
        sp_client: SpotifyClient,
    ) -> None:
        self._tracks = track_repo
        self._log = sync_log
        self._yt = yt_client
        self._sp = sp_client

    async def process_pending(self) -> int:
        pending = await self._tracks.get_pending_tracks("yt_to_sp")
        if not pending:
            return 0

        logger.info("yt_to_sp_processing", count=len(pending))
        processed = 0
        playlist_tracks = self._sp.get_playlist_tracks()

        for track in pending:
            try:
                await self._process_track(track, playlist_tracks)
                processed += 1
            except Exception:
                logger.error(
                    "yt_to_sp_track_error",
                    track_id=track.id,
                    exc_info=True,
                )
                await self._tracks.mark_failed(track.id, "unexpected_error")
                await self._log.log(
                    "sync_failed",
                    track_id=track.id,
                    direction="yt_to_sp",
                    details={"reason": "unexpected_error"},
                )
                SYNC_ERRORS.labels(direction="yt_to_sp", error_type="unexpected").inc()

        return processed

    async def _process_track(self, track, playlist_tracks: list[dict]) -> None:
        start = time.monotonic()
        await self._tracks.update_track(track.id, status="searching")

        # Search Spotify by artist/title
        search_start = time.monotonic()
        match = self._sp.find_best_match(track.artist, track.title)
        SP_SEARCH_DURATION.observe(time.monotonic() - search_start)

        if not match:
            await self._tracks.mark_failed(track.id, "no_sp_match")
            await self._log.log(
                "no_sp_match",
                track_id=track.id,
                direction="yt_to_sp",
                details={"artist": track.artist, "title": track.title},
            )
            SP_SEARCH_TOTAL.labels(result="not_found").inc()
            SYNC_ERRORS.labels(direction="yt_to_sp", error_type="no_sp_match").inc()
            return

        SP_SEARCH_TOTAL.labels(result="found").inc()
        sp_track_id = match["id"]

        # Check for duplicates
        if self._sp.is_in_playlist(sp_track_id, playlist_tracks):
            await self._tracks.mark_duplicate(track.id)
            await self._tracks.update_track(track.id, sp_track_id=sp_track_id)
            await self._log.log(
                "duplicate_skipped",
                track_id=track.id,
                direction="yt_to_sp",
                details={"sp_track_id": sp_track_id},
            )
            DUPLICATES_SKIPPED.labels(direction="yt_to_sp").inc()
            logger.info("duplicate_skipped", track_id=track.id, sp_track_id=sp_track_id)
            return

        # Add to Spotify playlist
        await self._tracks.update_track(track.id, status="syncing")
        self._sp.add_to_playlist(sp_track_id)

        await self._tracks.mark_synced(track.id, sp_track_id=sp_track_id)
        await self._log.log(
            "track_synced",
            track_id=track.id,
            direction="yt_to_sp",
            details={"sp_track_id": sp_track_id, "name": match.get("name")},
        )

        TRACKS_SYNCED.labels(direction="yt_to_sp").inc()
        TRACK_SYNC_DURATION.labels(direction="yt_to_sp").observe(time.monotonic() - start)
        logger.info(
            "yt_to_sp_synced",
            track_id=track.id,
            sp_track_id=sp_track_id,
            name=match.get("name"),
        )
