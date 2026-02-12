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
from navaar.telegram.client import TelegramClient
from navaar.ytmusic.client import YTMusicClient
from navaar.ytmusic.downloader import YTDownloader

logger = structlog.get_logger()

SNAPSHOT_KEY = "yt_playlist_snapshot"


class YtToTgSync:
    def __init__(
        self,
        track_repo: TrackRepository,
        sync_state: SyncStateRepository,
        sync_log: SyncLogRepository,
        tg_client: TelegramClient,
        yt_client: YTMusicClient,
        downloader: YTDownloader,
    ) -> None:
        self._tracks = track_repo
        self._state = sync_state
        self._log = sync_log
        self._tg = tg_client
        self._yt = yt_client
        self._dl = downloader

    async def process_new_tracks(self) -> int:
        synced = 0

        # Part 1: Process retry_scheduled tracks
        retries = await self._tracks.get_pending_tracks("yt_to_tg")
        for track in retries:
            if not track.yt_video_id:
                continue
            try:
                await self._retry_track(track)
                synced += 1
            except Exception:
                logger.error("yt_to_tg_retry_error", track_id=track.id, exc_info=True)
                SYNC_ERRORS.labels(direction="yt_to_tg", error_type="retry_failed").inc()

        # Part 2: Diff playlist for new tracks
        playlist_tracks = self._yt.get_playlist_tracks()
        current_ids = [t["videoId"] for t in playlist_tracks if t.get("videoId")]

        prev_snapshot = await self._state.get_json(SNAPSHOT_KEY)
        prev_ids = set(prev_snapshot) if isinstance(prev_snapshot, list) else set()

        new_ids = [vid for vid in current_ids if vid not in prev_ids]

        if new_ids:
            logger.info("yt_to_tg_new_tracks", count=len(new_ids))
            track_lookup = {t["videoId"]: t for t in playlist_tracks if t.get("videoId")}

            for video_id in new_ids:
                try:
                    await self._sync_track(video_id, track_lookup.get(video_id, {}))
                    synced += 1
                except Exception:
                    logger.error("yt_to_tg_track_error", video_id=video_id, exc_info=True)
                    SYNC_ERRORS.labels(direction="yt_to_tg", error_type="sync_failed").inc()

        # Update snapshot
        await self._state.set_json(SNAPSHOT_KEY, current_ids)
        return synced

    async def _retry_track(self, track) -> None:
        """Re-attempt download + upload for a previously failed yt_to_tg track."""
        start = time.monotonic()
        logger.info("yt_to_tg_retrying", track_id=track.id, video_id=track.yt_video_id)
        await self._tracks.update_track(track.id, status="syncing")

        local_path = None
        try:
            local_path = await self._dl.download(track.yt_video_id)
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
            TRACKS_SYNCED.labels(direction="yt_to_tg").inc()
            TRACK_SYNC_DURATION.labels(direction="yt_to_tg").observe(time.monotonic() - start)
            logger.info("yt_to_tg_retry_synced", track_id=track.id, message_id=message_id)
        except Exception as e:
            TG_UPLOAD_TOTAL.labels(result="failure").inc()
            await self._tracks.mark_failed(track.id, f"upload_failed: {e}")
            raise
        finally:
            self._dl.cleanup(local_path)

    async def _sync_track(self, video_id: str, yt_meta: dict) -> None:
        start = time.monotonic()

        # Check if already synced
        existing = await self._tracks.get_track_by_yt_video_id(video_id)
        if existing and existing.status in ("synced", "duplicate"):
            logger.debug("yt_to_tg_already_synced", video_id=video_id)
            return

        title = yt_meta.get("title", video_id)
        artists = yt_meta.get("artists", [])
        artist = artists[0]["name"] if artists else None
        duration = yt_meta.get("duration_seconds")

        # Create track record
        track = await self._tracks.create_track(
            direction="yt_to_tg",
            status="pending",
            title=title,
            artist=artist,
            yt_video_id=video_id,
            yt_set_video_id=yt_meta.get("setVideoId"),
            duration_seconds=duration,
            identification_method="yt_metadata",
        )
        TRACKS_DISCOVERED.labels(direction="yt_to_tg").inc()

        # Download
        await self._tracks.update_track(track.id, status="syncing")
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
                direction="yt_to_tg",
                details={"video_id": video_id, "error": str(e)},
            )
            raise

        # Upload to Telegram
        try:
            caption = f"Synced by Navaar | #{track.id}"
            message_id = await self._tg.send_audio(
                file_path=local_path,
                title=title,
                performer=artist,
                duration=duration,
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
                direction="yt_to_tg",
                details={
                    "video_id": video_id,
                    "message_id": message_id,
                    "title": title,
                },
            )
            TRACKS_SYNCED.labels(direction="yt_to_tg").inc()
            TRACK_SYNC_DURATION.labels(direction="yt_to_tg").observe(time.monotonic() - start)
            logger.info(
                "yt_to_tg_synced",
                track_id=track.id,
                video_id=video_id,
                message_id=message_id,
            )
        except Exception as e:
            TG_UPLOAD_TOTAL.labels(result="failure").inc()
            await self._tracks.mark_failed(track.id, f"upload_failed: {e}")
            await self._log.log(
                "upload_failed",
                track_id=track.id,
                direction="yt_to_tg",
                details={"video_id": video_id, "error": str(e)},
            )
            raise
        finally:
            self._dl.cleanup(local_path)
