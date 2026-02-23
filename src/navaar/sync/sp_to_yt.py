from __future__ import annotations

import time

import structlog

from navaar.db.repository import SyncLogRepository, TrackRepository
from navaar.metrics import (
    DUPLICATES_SKIPPED,
    SYNC_ERRORS,
    TRACK_SYNC_DURATION,
    TRACKS_SYNCED,
    YT_SEARCH_DURATION,
    YT_SEARCH_TOTAL,
)
from navaar.spotify.client import SpotifyClient
from navaar.ytmusic.client import YTMusicClient

logger = structlog.get_logger()


class SpToYtSync:
    def __init__(
        self,
        track_repo: TrackRepository,
        sync_log: SyncLogRepository,
        sp_client: SpotifyClient,
        yt_client: YTMusicClient,
    ) -> None:
        self._tracks = track_repo
        self._log = sync_log
        self._sp = sp_client
        self._yt = yt_client

    async def process_pending(self) -> int:
        pending = await self._tracks.get_pending_tracks("sp_to_yt")
        if not pending:
            return 0

        logger.info("sp_to_yt_processing", count=len(pending))
        processed = 0
        playlist_tracks = self._yt.get_playlist_tracks()

        for track in pending:
            try:
                await self._process_track(track, playlist_tracks)
                processed += 1
            except Exception:
                logger.error(
                    "sp_to_yt_track_error",
                    track_id=track.id,
                    exc_info=True,
                )
                await self._tracks.mark_failed(track.id, "unexpected_error")
                await self._log.log(
                    "sync_failed",
                    track_id=track.id,
                    direction="sp_to_yt",
                    details={"reason": "unexpected_error"},
                )
                SYNC_ERRORS.labels(direction="sp_to_yt", error_type="unexpected").inc()

        return processed

    async def _process_track(self, track, playlist_tracks: list[dict]) -> None:
        start = time.monotonic()
        await self._tracks.update_track(track.id, status="searching")

        # Search YouTube by artist/title
        search_start = time.monotonic()
        match = self._yt.find_best_match(track.artist, track.title)
        YT_SEARCH_DURATION.observe(time.monotonic() - search_start)

        if not match:
            await self._tracks.mark_failed(track.id, "no_yt_match")
            await self._log.log(
                "no_yt_match",
                track_id=track.id,
                direction="sp_to_yt",
                details={"artist": track.artist, "title": track.title},
            )
            YT_SEARCH_TOTAL.labels(result="not_found").inc()
            SYNC_ERRORS.labels(direction="sp_to_yt", error_type="no_yt_match").inc()
            return

        YT_SEARCH_TOTAL.labels(result="found").inc()
        video_id = match["videoId"]

        # Check for duplicates
        if self._yt.is_in_playlist(video_id, playlist_tracks):
            await self._tracks.mark_duplicate(track.id)
            await self._tracks.update_track(track.id, yt_video_id=video_id)
            await self._log.log(
                "duplicate_skipped",
                track_id=track.id,
                direction="sp_to_yt",
                details={"video_id": video_id},
            )
            DUPLICATES_SKIPPED.labels(direction="sp_to_yt").inc()
            logger.info("duplicate_skipped", track_id=track.id, video_id=video_id)
            return

        # Add to YT playlist
        await self._tracks.update_track(track.id, status="syncing")
        self._yt.add_to_playlist(video_id)

        await self._tracks.mark_synced(track.id, yt_video_id=video_id)
        await self._log.log(
            "track_synced",
            track_id=track.id,
            direction="sp_to_yt",
            details={"video_id": video_id, "title": match.get("title")},
        )

        TRACKS_SYNCED.labels(direction="sp_to_yt").inc()
        TRACK_SYNC_DURATION.labels(direction="sp_to_yt").observe(time.monotonic() - start)
        logger.info(
            "sp_to_yt_synced",
            track_id=track.id,
            video_id=video_id,
            title=match.get("title"),
        )
