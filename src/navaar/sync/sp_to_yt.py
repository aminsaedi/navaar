from __future__ import annotations

from navaar.db.repository import SyncLogRepository, TrackRepository
from navaar.spotify.client import SpotifyClient
from navaar.sync._base_push import BasePushSync
from navaar.sync._targets import YT_TARGET
from navaar.ytmusic.client import YTMusicClient


class SpToYtSync(BasePushSync):
    direction = "sp_to_yt"
    target = YT_TARGET
    identify_from_telegram = False

    def __init__(
        self,
        track_repo: TrackRepository,
        sync_log: SyncLogRepository,
        sp_client: SpotifyClient,  # source service; metadata already on the track
        yt_client: YTMusicClient,
    ) -> None:
        super().__init__(track_repo, sync_log, yt_client)
