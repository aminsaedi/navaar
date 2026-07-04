from __future__ import annotations

import asyncio
import time

import structlog

from navaar.db.repository import SyncLogRepository, SyncStateRepository, TrackRepository
from navaar.metrics import (
    SYNC_ERRORS,
    TG_UPLOAD_TOTAL,
    TRACK_SYNC_DURATION,
    TRACKS_SYNCED,
    YT_DOWNLOAD_TOTAL,
)
from navaar.services import sync_caption
from navaar.telegram.client import TelegramClient
from navaar.ytmusic.downloader import YTDownloader

logger = structlog.get_logger()


class BasePullSync:
    """Shared skeleton for the two pull directions (yt→tg, sp→tg): retry failed
    tracks, then diff the source playlist against a stored snapshot and process
    new ids. Both download audio from YouTube and upload to Telegram — that flow
    lives in ``_download_and_upload``. Subclasses provide the source-specific bits
    (snapshot key, id key/field, the playlist client, and how a new/retried track
    is turned into a downloadable YouTube video id).
    """

    direction: str
    snapshot_key: str
    id_key: str    # key of the external id in a playlist item dict
    id_field: str  # Track column holding that external id

    def __init__(
        self,
        track_repo: TrackRepository,
        sync_state: SyncStateRepository,
        sync_log: SyncLogRepository,
        tg_client: TelegramClient,
        downloader: YTDownloader,
    ) -> None:
        self._tracks = track_repo
        self._state = sync_state
        self._log = sync_log
        self._tg = tg_client
        self._dl = downloader
        self._card = None

    # Subclasses set this to the client whose get_playlist_tracks() is diffed.
    _playlist_client: object

    def set_card_service(self, card_service: object) -> None:
        self._card = card_service

    async def _emit_card(self, track_id: int) -> None:
        if self._card is not None:
            await self._card.refresh(track_id)

    async def process_new_tracks(self) -> int:
        synced = 0

        # Part 1: retry previously-failed tracks for this direction.
        retries = await self._tracks.get_pending_tracks(self.direction)
        for track in retries:
            if not getattr(track, self.id_field):
                continue
            try:
                await self._retry_track(track)
                synced += 1
            except Exception:
                logger.error(f"{self.direction}_retry_error", track_id=track.id, exc_info=True)
                SYNC_ERRORS.labels(direction=self.direction, error_type="retry_failed").inc()

        # Part 2: diff the playlist against the stored snapshot.
        playlist_tracks = await asyncio.to_thread(self._playlist_client.get_playlist_tracks)
        current_ids = [t[self.id_key] for t in playlist_tracks if t.get(self.id_key)]

        prev_snapshot = await self._state.get_json(self.snapshot_key)

        # First observation: no snapshot has ever been stored (None, not []). Seed
        # it with the whole current playlist and process nothing — otherwise every
        # pre-existing track would be treated as "new" and mass-downloaded into the
        # channel. This is the first-boot / Spotify-just-enabled case; the group
        # never asked for a bulk import of songs that predate Navaar.
        if prev_snapshot is None:
            logger.info(
                f"{self.direction}_snapshot_seeded", count=len(current_ids),
            )
            await self._state.set_json(self.snapshot_key, current_ids)
            return synced

        prev_ids = set(prev_snapshot) if isinstance(prev_snapshot, list) else set()
        new_ids = [i for i in current_ids if i not in prev_ids]

        # Track which new ids are "handled" and should stay in the snapshot. An id is
        # handled when _sync_new returns normally — whether it ingested the track OR
        # deliberately skipped an id already handled elsewhere in the mesh (a pushed /
        # fanned-out id, the common steady state, which has no row for THIS pull
        # direction). An id whose _sync_new *raised before creating any row* (e.g. a
        # transient DB error) is left OUT so the next cycle re-diffs and retries it,
        # instead of the track being permanently and silently lost.
        recorded: set[str] = set()
        if new_ids:
            logger.info(f"{self.direction}_new_tracks", count=len(new_ids))
            lookup = {t[self.id_key]: t for t in playlist_tracks if t.get(self.id_key)}
            for new_id in new_ids:
                raised = False
                try:
                    await self._sync_new(new_id, lookup.get(new_id, {}))
                    synced += 1
                except Exception:
                    raised = True
                    logger.error(
                        f"{self.direction}_track_error",
                        external_id=new_id,
                        exc_info=True,
                    )
                    SYNC_ERRORS.labels(direction=self.direction, error_type="sync_failed").inc()
                # Normal return → handled → record. A raise that still left a row (e.g.
                # a download failure after create_track) is also recorded — Part-1 /
                # manual retry owns recovery. Only a raise with NO row anywhere is
                # omitted, so the id is retried via the diff next cycle.
                if not raised or await self._row_exists(new_id):
                    recorded.add(new_id)

        # Advance the snapshot to: ids already known (in prev_ids) + newly-recorded
        # ids, intersected with what's still present. Un-ingested new ids are omitted
        # so they're retried; ids removed from the playlist naturally drop out.
        keep = prev_ids | recorded
        next_snapshot = [i for i in current_ids if i in keep]
        await self._state.set_json(self.snapshot_key, next_snapshot)
        return synced

    async def _download_and_upload(
        self,
        *,
        track_id: int,
        video_id: str,
        title: str | None,
        artist: str | None,
        duration: int | None,
        start: float,
    ) -> int:
        """Download a YouTube video via yt-dlp, upload it to Telegram, mark the
        track synced, and return the Telegram message id. Marks the track failed
        and re-raises on download or upload failure. Refreshes the track's status
        card on every terminal outcome (the upload creates the card's anchor)."""
        try:
            local_path = None
            try:
                local_path = await self._dl.download(video_id)
                YT_DOWNLOAD_TOTAL.labels(result="success").inc()
            except Exception as e:
                YT_DOWNLOAD_TOTAL.labels(result="failure").inc()
                await self._tracks.mark_failed(track_id, f"download_failed: {e}")
                await self._log.log(
                    "download_failed",
                    track_id=track_id,
                    direction=self.direction,
                    details={"video_id": video_id, "error": str(e)},
                )
                raise

            try:
                caption = sync_caption(self.direction, track_id)
                message_id = await self._tg.send_audio(
                    file_path=local_path,
                    title=title,
                    performer=artist,
                    duration=duration,
                    caption=caption,
                )
                TG_UPLOAD_TOTAL.labels(result="success").inc()
                await self._tracks.mark_synced(track_id, tg_message_id=message_id)
                await self._log.log(
                    "track_synced",
                    track_id=track_id,
                    direction=self.direction,
                    details={"video_id": video_id, "message_id": message_id, "title": title},
                )
                TRACKS_SYNCED.labels(direction=self.direction).inc()
                TRACK_SYNC_DURATION.labels(direction=self.direction).observe(time.monotonic() - start)
                logger.info(f"{self.direction}_synced", track_id=track_id, message_id=message_id)
                return message_id
            except Exception as e:
                TG_UPLOAD_TOTAL.labels(result="failure").inc()
                await self._tracks.mark_failed(track_id, f"upload_failed: {e}")
                await self._log.log(
                    "upload_failed",
                    track_id=track_id,
                    direction=self.direction,
                    details={"video_id": video_id, "error": str(e)},
                )
                raise
            finally:
                self._dl.cleanup(local_path)
        finally:
            await self._emit_card(track_id)

    async def _row_exists(self, external_id: str) -> bool:
        """Does ANY row (any direction) already track this external id? Used to decide
        whether a raised _sync_new left a recoverable row behind. Resilient: a failure
        of the lookup itself must not crash the cycle — return False so the id is
        retried next cycle rather than lost."""
        try:
            if self.id_field == "yt_video_id":
                return await self._tracks.get_track_by_yt_video_id(external_id) is not None
            if self.id_field == "sp_track_id":
                return await self._tracks.get_track_by_sp_track_id(external_id) is not None
        except Exception:
            return False
        return False

    async def _retry_track(self, track) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    async def _sync_new(self, external_id: str, meta: dict) -> None:  # pragma: no cover
        raise NotImplementedError
