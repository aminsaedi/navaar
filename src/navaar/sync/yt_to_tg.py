from __future__ import annotations

import time

import structlog

from navaar.db.repository import SyncLogRepository, SyncStateRepository, TrackRepository
from navaar.metrics import TRACKS_DISCOVERED
from navaar.sync._base_pull import BasePullSync
from navaar.sync.fanout import FanOut
from navaar.telegram.client import TelegramClient
from navaar.ytmusic.client import YTMusicClient
from navaar.ytmusic.downloader import YTDownloader

logger = structlog.get_logger()

SNAPSHOT_KEY = "yt_playlist_snapshot"


class YtToTgSync(BasePullSync):
    direction = "yt_to_tg"
    snapshot_key = SNAPSHOT_KEY
    id_key = "videoId"
    id_field = "yt_video_id"

    def __init__(
        self,
        track_repo: TrackRepository,
        sync_state: SyncStateRepository,
        sync_log: SyncLogRepository,
        tg_client: TelegramClient,
        yt_client: YTMusicClient,
        downloader: YTDownloader,
        sp_enabled: bool = False,
    ) -> None:
        super().__init__(track_repo, sync_state, sync_log, tg_client, downloader)
        self._yt = yt_client
        self._playlist_client = yt_client
        self._fanout = FanOut(track_repo, sp_enabled=sp_enabled)

    async def _retry_track(self, track) -> None:
        start = time.monotonic()
        logger.info("yt_to_tg_retrying", track_id=track.id, video_id=track.yt_video_id)
        await self._tracks.update_track(track.id, status="syncing")
        await self._download_and_upload(
            track_id=track.id,
            video_id=track.yt_video_id,
            title=track.title,
            artist=track.artist,
            duration=track.duration_seconds,
            start=start,
        )

    async def _sync_new(self, video_id: str, meta: dict) -> None:
        start = time.monotonic()

        existing = await self._tracks.get_track_by_yt_video_id(video_id)
        if existing and existing.status in ("synced", "duplicate"):
            logger.debug("yt_to_tg_already_synced", video_id=video_id)
            return

        title = meta.get("title", video_id)
        artists = meta.get("artists", [])
        artist = artists[0]["name"] if artists else None
        duration = meta.get("duration_seconds")

        track = await self._tracks.create_track(
            direction="yt_to_tg",
            status="pending",
            title=title,
            artist=artist,
            yt_video_id=video_id,
            yt_set_video_id=meta.get("setVideoId"),
            duration_seconds=duration,
            identification_method="yt_metadata",
        )
        TRACKS_DISCOVERED.labels(direction="yt_to_tg").inc()

        await self._fanout.from_youtube(
            video_id=video_id, title=title, artist=artist, duration=duration
        )

        await self._tracks.update_track(track.id, status="syncing")
        await self._download_and_upload(
            track_id=track.id,
            video_id=video_id,
            title=title,
            artist=artist,
            duration=duration,
            start=start,
        )
