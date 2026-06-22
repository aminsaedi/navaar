from __future__ import annotations

from navaar.db.repository import SyncLogRepository, TrackRepository
from navaar.spotify.client import SpotifyClient
from navaar.sync._base_push import BasePushSync
from navaar.sync._targets import SP_TARGET
from navaar.telegram.client import TelegramClient


class TgToSpSync(BasePushSync):
    direction = "tg_to_sp"
    target = SP_TARGET
    identify_from_telegram = True

    def __init__(
        self,
        track_repo: TrackRepository,
        sync_log: SyncLogRepository,
        tg_client: TelegramClient,
        sp_client: SpotifyClient,
    ) -> None:
        super().__init__(track_repo, sync_log, sp_client, tg_client=tg_client)
