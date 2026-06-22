from __future__ import annotations

from navaar.db.repository import SyncLogRepository, TrackRepository
from navaar.sync._base_push import BasePushSync
from navaar.sync._targets import YT_TARGET
from navaar.telegram.client import TelegramClient
from navaar.ytmusic.client import YTMusicClient


class TgToYtSync(BasePushSync):
    direction = "tg_to_yt"
    target = YT_TARGET
    identify_from_telegram = True

    def __init__(
        self,
        track_repo: TrackRepository,
        sync_log: SyncLogRepository,
        tg_client: TelegramClient,
        yt_client: YTMusicClient,
    ) -> None:
        super().__init__(track_repo, sync_log, yt_client, tg_client=tg_client)
