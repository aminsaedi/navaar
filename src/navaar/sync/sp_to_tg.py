from __future__ import annotations

import time

import structlog

from navaar.db.repository import SyncLogRepository, SyncStateRepository, TrackRepository
from navaar.metrics import (
    SYNC_ERRORS,
    TG_UPLOAD_TOTAL,
    TRACK_SYNC_DURATION,
    TRACKS_DISCOVERED,
    TRACKS_SYNCED,
    YT_DOWNLOAD_TOTAL,
)
from navaar.spotify.client import SpotifyClient
from navaar.telegram.client import TelegramClient
from navaar.ytmusic.client import YTMusicClient
from navaar.ytmusic.downloader import YTDownloader

logger = structlog.get_logger()

SNAPSHOT_KEY = "sp_playlist_snapshot"


class SpToTgSync:
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
        self._tracks = track_repo
        self._state = sync_state
        self._log = sync_log
        self._tg = tg_client
        self._sp = sp_client
        self._yt = yt_client
        self._dl = downloader

    async def process_new_tracks(self) -> int:
        synced = 0

        # Part 1: Process retry_scheduled tracks
        retries = await self._tracks.get_pending_tracks("sp_to_tg")
        for track in retries:
            if not track.sp_track_id:
                continue
            try:
                await self._retry_track(track)
                synced += 1
            except Exception:
                logger.error("sp_to_tg_retry_error", track_id=track.id, exc_info=True)
                SYNC_ERRORS.labels(direction="sp_to_tg", error_type="retry_failed").inc()

        # Part 2: Diff playlist for new tracks
        playlist_tracks = self._sp.get_playlist_tracks()
        current_ids = [t["id"] for t in playlist_tracks if t.get("id")]

        prev_snapshot = await self._state.get_json(SNAPSHOT_KEY)
        prev_ids = set(prev_snapshot) if isinstance(prev_snapshot, list) else set()

        new_ids = [tid for tid in current_ids if tid not in prev_ids]

        if new_ids:
            logger.info("sp_to_tg_new_tracks", count=len(new_ids))
            track_lookup = {t["id"]: t for t in playlist_tracks if t.get("id")}

            for sp_track_id in new_ids:
                try:
                    await self._sync_track(sp_track_id, track_lookup.get(sp_track_id, {}))
                    synced += 1
                except Exception:
                    logger.error("sp_to_tg_track_error", sp_track_id=sp_track_id, exc_info=True)
                    SYNC_ERRORS.labels(direction="sp_to_tg", error_type="sync_failed").inc()

        # Update snapshot
        await self._state.set_json(SNAPSHOT_KEY, current_ids)
        return synced

    async def _retry_track(self, track) -> None:
        """Re-attempt download + upload for a previously failed sp_to_tg track."""
        start = time.monotonic()
        logger.info("sp_to_tg_retrying", track_id=track.id, sp_track_id=track.sp_track_id)
        await self._tracks.update_track(track.id, status="syncing")

        # Search YouTube for this track to get audio
        yt_match = self._yt.find_best_match(track.artist, track.title)
        if not yt_match:
            await self._tracks.mark_failed(track.id, "no_yt_match_for_download")
            raise RuntimeError(f"No YT match for SP track {track.sp_track_id}")

        video_id = yt_match["videoId"]
        local_path = None
        try:
            local_path = await self._dl.download(video_id)
            YT_DOWNLOAD_TOTAL.labels(result="success").inc()
        except Exception as e:
            YT_DOWNLOAD_TOTAL.labels(result="failure").inc()
            await self._tracks.mark_failed(track.id, f"download_failed: {e}")
            raise

        try:
            caption = f"Synced by Navaar | #{track.id}"
            message_id = await self._tg.send_audio(
                file_path=local_path,
                title=track.title,
                performer=track.artist,
                duration=track.duration_seconds,
                caption=caption,
            )
            TG_UPLOAD_TOTAL.labels(result="success").inc()
            await self._tracks.mark_synced(track.id, tg_message_id=message_id)
            TRACKS_SYNCED.labels(direction="sp_to_tg").inc()
            TRACK_SYNC_DURATION.labels(direction="sp_to_tg").observe(time.monotonic() - start)
            logger.info("sp_to_tg_retry_synced", track_id=track.id, message_id=message_id)
        except Exception as e:
            TG_UPLOAD_TOTAL.labels(result="failure").inc()
            await self._tracks.mark_failed(track.id, f"upload_failed: {e}")
            raise
        finally:
            self._dl.cleanup(local_path)

    async def _sync_track(self, sp_track_id: str, sp_meta: dict) -> None:
        start = time.monotonic()

        # Check if already synced (cross-service dedup)
        existing = await self._tracks.get_track_by_sp_track_id(sp_track_id)
        if existing and existing.status in ("synced", "duplicate"):
            logger.debug("sp_to_tg_already_synced", sp_track_id=sp_track_id)
            return

        name = sp_meta.get("name", sp_track_id)
        artists = sp_meta.get("artists", [])
        artist = artists[0] if artists else None
        duration_ms = sp_meta.get("duration_ms")
        duration_seconds = duration_ms // 1000 if duration_ms else None

        # Create sp_to_tg track record
        track = await self._tracks.create_track(
            direction="sp_to_tg",
            status="pending",
            title=name,
            artist=artist,
            sp_track_id=sp_track_id,
            duration_seconds=duration_seconds,
            identification_method="sp_metadata",
        )
        TRACKS_DISCOVERED.labels(direction="sp_to_tg").inc()

        # Fan-out: also create sp_to_yt track
        existing_yt = await self._tracks.get_track_by_sp_track_id(sp_track_id)
        # Only create sp_to_yt if no other track with this sp_track_id is already synced to YT
        should_fan_out = True
        if existing_yt and existing_yt.id != track.id:
            should_fan_out = False
        if should_fan_out:
            await self._tracks.create_track(
                direction="sp_to_yt",
                status="pending",
                title=name,
                artist=artist,
                sp_track_id=sp_track_id,
                duration_seconds=duration_seconds,
                identification_method="sp_metadata",
            )
            TRACKS_DISCOVERED.labels(direction="sp_to_yt").inc()

        # Search YouTube for this track to get audio
        yt_match = self._yt.find_best_match(artist, name)
        if not yt_match:
            await self._tracks.mark_failed(track.id, "no_yt_match_for_download")
            await self._log.log(
                "no_yt_match_for_download",
                track_id=track.id,
                direction="sp_to_tg",
                details={"sp_track_id": sp_track_id, "name": name},
            )
            SYNC_ERRORS.labels(direction="sp_to_tg", error_type="no_yt_match").inc()
            raise RuntimeError(f"No YT match for SP track {sp_track_id}")

        video_id = yt_match["videoId"]

        # Download from YouTube
        await self._tracks.update_track(track.id, status="syncing", yt_video_id=video_id)
        local_path = None
        try:
            local_path = await self._dl.download(video_id)
            YT_DOWNLOAD_TOTAL.labels(result="success").inc()
        except Exception as e:
            YT_DOWNLOAD_TOTAL.labels(result="failure").inc()
            await self._tracks.mark_failed(track.id, f"download_failed: {e}")
            await self._log.log(
                "download_failed",
                track_id=track.id,
                direction="sp_to_tg",
                details={"video_id": video_id, "error": str(e)},
            )
            raise

        # Upload to Telegram
        try:
            caption = f"Synced by Navaar | #{track.id}"
            message_id = await self._tg.send_audio(
                file_path=local_path,
                title=name,
                performer=artist,
                duration=duration_seconds,
                caption=caption,
            )
            TG_UPLOAD_TOTAL.labels(result="success").inc()

            await self._tracks.mark_synced(
                track.id,
                tg_message_id=message_id,
            )
            await self._log.log(
                "track_synced",
                track_id=track.id,
                direction="sp_to_tg",
                details={
                    "sp_track_id": sp_track_id,
                    "message_id": message_id,
                    "name": name,
                },
            )
            TRACKS_SYNCED.labels(direction="sp_to_tg").inc()
            TRACK_SYNC_DURATION.labels(direction="sp_to_tg").observe(time.monotonic() - start)
            logger.info(
                "sp_to_tg_synced",
                track_id=track.id,
                sp_track_id=sp_track_id,
                message_id=message_id,
            )
        except Exception as e:
            TG_UPLOAD_TOTAL.labels(result="failure").inc()
            await self._tracks.mark_failed(track.id, f"upload_failed: {e}")
            await self._log.log(
                "upload_failed",
                track_id=track.id,
                direction="sp_to_tg",
                details={"sp_track_id": sp_track_id, "error": str(e)},
            )
            raise
        finally:
            self._dl.cleanup(local_path)
