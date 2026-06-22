from __future__ import annotations

from navaar.db.repository import SyncLogRepository, TrackRepository
from navaar.spotify.client import SpotifyClient
from navaar.sync._base_push import BasePushSync
from navaar.sync._targets import SP_TARGET
from navaar.ytmusic.client import YTMusicClient


class YtToSpSync(BasePushSync):
    direction = "yt_to_sp"
    target = SP_TARGET
    identify_from_telegram = False

    def __init__(
        self,
        track_repo: TrackRepository,
        sync_log: SyncLogRepository,
        yt_client: YTMusicClient,  # source service; metadata already on the track
        sp_client: SpotifyClient,
    ) -> None:
        super().__init__(track_repo, sync_log, sp_client)
