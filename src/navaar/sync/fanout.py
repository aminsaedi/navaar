from __future__ import annotations

import structlog

from navaar.db.repository import TrackRepository
from navaar.metrics import TRACKS_DISCOVERED

logger = structlog.get_logger()


class FanOut:
    """Centralizes the "a track arrived from one service → create pending tracks
    for the *other* targets" policy, with one consistent cross-service dedup rule.

    Previously this lived in three places (the bot, yt_to_tg, sp_to_tg) with three
    different dedup strategies (one of which had none), which risked duplicate rows
    and sync loops. Each method here creates only the secondary target(s) — the
    primary/source track is created by the caller that already deduped it.
    """

    def __init__(self, track_repo: TrackRepository, *, sp_enabled: bool) -> None:
        self._tracks = track_repo
        self._sp_enabled = sp_enabled

    async def _create(self, direction: str, **fields: object) -> None:
        await self._tracks.create_track(direction=direction, status="pending", **fields)
        TRACKS_DISCOVERED.labels(direction=direction).inc()
        logger.info("fanout_track_created", direction=direction)

    async def from_telegram(
        self, *, tg_file_id: str, title: str, artist: str | None, duration: int | None
    ) -> None:
        """A Telegram channel post fans out to Spotify (the YT target is the primary)."""
        if not self._sp_enabled:
            return
        if await self._tracks.has_track_for_direction("tg_to_sp", tg_file_id=tg_file_id):
            return
        await self._create(
            "tg_to_sp", title=title, artist=artist, tg_file_id=tg_file_id,
            duration_seconds=duration,
        )

    async def from_youtube(
        self, *, video_id: str, title: str, artist: str | None, duration: int | None
    ) -> None:
        """A new YouTube playlist track fans out to Spotify."""
        if not self._sp_enabled:
            return
        if await self._tracks.has_track_for_direction("yt_to_sp", yt_video_id=video_id):
            return
        await self._create(
            "yt_to_sp", title=title, artist=artist, yt_video_id=video_id,
            duration_seconds=duration, identification_method="yt_metadata",
        )

    async def from_spotify(
        self, *, sp_track_id: str, title: str, artist: str | None, duration: int | None
    ) -> None:
        """A new Spotify playlist track fans out to YouTube (Spotify implies enabled)."""
        if await self._tracks.has_track_for_direction("sp_to_yt", sp_track_id=sp_track_id):
            return
        await self._create(
            "sp_to_yt", title=title, artist=artist, sp_track_id=sp_track_id,
            duration_seconds=duration, identification_method="sp_metadata",
        )
