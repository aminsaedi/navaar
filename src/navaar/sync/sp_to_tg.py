from __future__ import annotations

import asyncio
import time

import structlog

from navaar.db.repository import SyncLogRepository, SyncStateRepository, TrackRepository
from navaar.metrics import SYNC_ERRORS, TRACKS_DISCOVERED
from navaar.spotify.client import SpotifyClient
from navaar.sync._base_pull import BasePullSync
from navaar.sync.fanout import FanOut
from navaar.telegram.client import TelegramClient
from navaar.ytmusic.client import YTMusicClient
from navaar.ytmusic.downloader import YTDownloader

logger = structlog.get_logger()

SNAPSHOT_KEY = "sp_playlist_snapshot"


class SpToTgSync(BasePullSync):
    direction = "sp_to_tg"
    snapshot_key = SNAPSHOT_KEY
    id_key = "id"
    id_field = "sp_track_id"

    def __init__(
        self,
        track_repo: TrackRepository,
        sync_state: SyncStateRepository,
        sync_log: SyncLogRepository,
        tg_client: TelegramClient,
        sp_client: SpotifyClient,
        yt_client: YTMusicClient,
        downloader: YTDownloader,
    ) -> None:
        super().__init__(track_repo, sync_state, sync_log, tg_client, downloader)
        self._sp = sp_client
        self._yt = yt_client
        self._playlist_client = sp_client
        # Spotify being active implies the full mesh, so fan-out to YT always runs.
        self._fanout = FanOut(track_repo, sp_enabled=True)

    async def _youtube_video_id(self, artist: str | None, title: str, track_id: int) -> str:
        """Spotify has no audio API, so find the same track on YouTube to download.
        Marks the track failed and raises if there's no match."""
        yt_match = await asyncio.to_thread(self._yt.find_best_match, artist, title)
        if not yt_match:
            await self._tracks.mark_failed(track_id, "no_yt_match_for_download")
            await self._log.log(
                "no_yt_match_for_download",
                track_id=track_id,
                direction="sp_to_tg",
                details={"artist": artist, "title": title},
            )
            SYNC_ERRORS.labels(direction="sp_to_tg", error_type="no_yt_match").inc()
            raise RuntimeError(f"No YT match for SP track (track_id={track_id})")
        return yt_match["videoId"]

    async def _retry_track(self, track) -> None:
        start = time.monotonic()
        logger.info("sp_to_tg_retrying", track_id=track.id, sp_track_id=track.sp_track_id)
        await self._tracks.update_track(track.id, status="syncing")
        video_id = await self._youtube_video_id(track.artist, track.title, track.id)
        await self._download_and_upload(
            track_id=track.id,
            video_id=video_id,
            title=track.title,
            artist=track.artist,
            duration=track.duration_seconds,
            start=start,
        )

    async def _sync_new(self, sp_track_id: str, meta: dict) -> None:
        start = time.monotonic()

        existing = await self._tracks.get_track_by_sp_track_id(sp_track_id)
        if existing and existing.status in ("synced", "duplicate"):
            logger.debug("sp_to_tg_already_synced", sp_track_id=sp_track_id)
            return

        name = meta.get("name", sp_track_id)
        artists = meta.get("artists", [])
        artist = artists[0] if artists else None
        duration_ms = meta.get("duration_ms")
        duration = duration_ms // 1000 if duration_ms else None

        track = await self._tracks.create_track(
            direction="sp_to_tg",
            status="pending",
            title=name,
            artist=artist,
            sp_track_id=sp_track_id,
            duration_seconds=duration,
            identification_method="sp_metadata",
        )
        TRACKS_DISCOVERED.labels(direction="sp_to_tg").inc()

        await self._fanout.from_spotify(
            sp_track_id=sp_track_id, title=name, artist=artist, duration=duration
        )

        video_id = await self._youtube_video_id(artist, name, track.id)
        await self._tracks.update_track(track.id, status="syncing", yt_video_id=video_id)
        await self._download_and_upload(
            track_id=track.id,
            video_id=video_id,
            title=name,
            artist=artist,
            duration=duration,
            start=start,
        )
