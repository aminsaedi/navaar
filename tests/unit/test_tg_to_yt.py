from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from navaar.db.repository import SyncLogRepository, TrackRepository
from navaar.sync.tg_to_yt import TgToYtSync
from navaar.telegram.client import FileTooBigError


@pytest.fixture
def mock_tg_client() -> MagicMock:
    client = MagicMock()
    client.download_file = AsyncMock(return_value="/tmp/test.mp3")
    client.cleanup = MagicMock()
    return client


@pytest.fixture
def mock_yt_client() -> MagicMock:
    client = MagicMock()
    client.get_playlist_tracks = MagicMock(return_value=[])
    client.find_best_match = MagicMock(return_value={"videoId": "abc123", "title": "Hello"})
    client.is_in_playlist = MagicMock(return_value=False)
    client.add_to_playlist = MagicMock(return_value="ok")
    return client


@pytest.mark.asyncio
async def test_process_pending_no_tracks(
    track_repo: TrackRepository,
    sync_log_repo: SyncLogRepository,
    mock_tg_client: MagicMock,
    mock_yt_client: MagicMock,
) -> None:
    sync = TgToYtSync(track_repo, sync_log_repo, mock_tg_client, mock_yt_client)
    result = await sync.process_pending()
    assert result == 0


@pytest.mark.asyncio
async def test_process_pending_syncs_track(
    track_repo: TrackRepository,
    sync_log_repo: SyncLogRepository,
    mock_tg_client: MagicMock,
    mock_yt_client: MagicMock,
) -> None:
    track = await track_repo.create_track(
        direction="tg_to_yt",
        status="pending",
        title="Hello",
        artist="Adele",
        tg_file_id="file_123",
    )

    with patch("navaar.sync._base_push.identify_track") as mock_identify:
        mock_identify.return_value = MagicMock(
            artist="Adele", title="Hello", method="tg_metadata"
        )
        sync = TgToYtSync(track_repo, sync_log_repo, mock_tg_client, mock_yt_client)
        result = await sync.process_pending()

    assert result == 1
    updated = await track_repo.get_track(track.id)
    assert updated.status == "synced"
    assert updated.yt_video_id == "abc123"


@pytest.mark.asyncio
async def test_process_pending_no_match(
    track_repo: TrackRepository,
    sync_log_repo: SyncLogRepository,
    mock_tg_client: MagicMock,
    mock_yt_client: MagicMock,
) -> None:
    mock_yt_client.find_best_match = MagicMock(return_value=None)

    await track_repo.create_track(
        direction="tg_to_yt",
        status="pending",
        title="asdfghjkl",
        tg_file_id="file_456",
    )

    with patch("navaar.sync._base_push.identify_track") as mock_identify:
        mock_identify.return_value = MagicMock(
            artist=None, title="asdfghjkl", method="filename"
        )
        sync = TgToYtSync(track_repo, sync_log_repo, mock_tg_client, mock_yt_client)
        result = await sync.process_pending()

    assert result == 1
    tracks = await track_repo.get_failed_tracks("tg_to_yt")
    assert len(tracks) == 1
    assert tracks[0].failure_reason == "no_yt_match"


@pytest.mark.asyncio
async def test_process_pending_duplicate(
    track_repo: TrackRepository,
    sync_log_repo: SyncLogRepository,
    mock_tg_client: MagicMock,
    mock_yt_client: MagicMock,
) -> None:
    mock_yt_client.is_in_playlist = MagicMock(return_value=True)

    track = await track_repo.create_track(
        direction="tg_to_yt",
        status="pending",
        title="Hello",
        artist="Adele",
        tg_file_id="file_789",
    )

    with patch("navaar.sync._base_push.identify_track") as mock_identify:
        mock_identify.return_value = MagicMock(
            artist="Adele", title="Hello", method="tg_metadata"
        )
        sync = TgToYtSync(track_repo, sync_log_repo, mock_tg_client, mock_yt_client)
        await sync.process_pending()

    updated = await track_repo.get_track(track.id)
    assert updated.status == "duplicate"


@pytest.mark.asyncio
async def test_process_pending_file_too_big_falls_back_to_metadata(
    track_repo: TrackRepository,
    sync_log_repo: SyncLogRepository,
    mock_tg_client: MagicMock,
    mock_yt_client: MagicMock,
) -> None:
    """A file over the Bot API's 20 MB getFile cap can never be downloaded, but the
    message's own performer/title still identify it — the track must still sync."""
    mock_tg_client.download_file = AsyncMock(side_effect=FileTooBigError("File is too big"))

    track = await track_repo.create_track(
        direction="tg_to_yt",
        status="pending",
        title="Shabe Meykhooneh",
        artist="Hayedeh",
        tg_file_id="file_big",
    )

    sync = TgToYtSync(track_repo, sync_log_repo, mock_tg_client, mock_yt_client)
    await sync.process_pending()

    updated = await track_repo.get_track(track.id)
    assert updated.status == "synced"
    assert updated.yt_video_id == "abc123"
    # Identified without the bytes, so no local file to clean up.
    mock_tg_client.cleanup.assert_not_called()


def _quota_exc():
    import httpx

    req = httpx.Request("GET", "https://www.googleapis.com/youtube/v3/search")
    resp = httpx.Response(
        403, request=req, json={"error": {"errors": [{"reason": "quotaExceeded"}]}}
    )
    return httpx.HTTPStatusError("Client error '403 Forbidden'", request=req, response=resp)


@pytest.mark.asyncio
async def test_quota_exhaustion_leaves_tracks_pending_and_pauses(
    track_repo: TrackRepository,
    sync_log_repo: SyncLogRepository,
    mock_tg_client: MagicMock,
    mock_yt_client: MagicMock,
) -> None:
    """A bulk post that runs the YouTube quota dry must not burn every remaining
    track to 'failed' (failed rows are never retried automatically). The cycle
    stops, the tracks stay pending, and later cycles make no API calls until the
    quota resets — the cycle still completes, so nothing crash-loops or alerts."""
    ids = [
        (
            await track_repo.create_track(
                direction="tg_to_yt", status="pending", title=f"Song {i}",
                artist="Artist", tg_file_id=f"file_{i}",
            )
        ).id
        for i in range(3)
    ]
    mock_yt_client.find_best_match = MagicMock(side_effect=_quota_exc())

    with patch("navaar.sync._base_push.identify_track") as mock_identify:
        mock_identify.return_value = MagicMock(artist="Artist", title="Song", method="id3")
        sync = TgToYtSync(track_repo, sync_log_repo, mock_tg_client, mock_yt_client)
        assert await sync.process_pending() == 0
        assert await sync.process_pending() == 0  # paused: no second attempt

    assert mock_yt_client.find_best_match.call_count == 1  # stopped at the first refusal
    assert mock_yt_client.get_playlist_tracks.call_count == 1
    for track_id in ids:
        track = await track_repo.get_track(track_id)
        assert track.status == "pending"
        assert track.retry_count == 0

    # Once the pause lapses (quota reset), the queue resumes by itself.
    sync._quota_paused_until = 0.0
    mock_yt_client.find_best_match = MagicMock(return_value={"videoId": "v1", "title": "Song"})
    with patch("navaar.sync._base_push.identify_track") as mock_identify:
        mock_identify.return_value = MagicMock(artist="Artist", title="Song", method="id3")
        assert await sync.process_pending() == 3


@pytest.mark.asyncio
async def test_quota_exhausted_on_playlist_fetch_pauses_without_crashing(
    track_repo: TrackRepository,
    sync_log_repo: SyncLogRepository,
    mock_tg_client: MagicMock,
    mock_yt_client: MagicMock,
) -> None:
    await track_repo.create_track(
        direction="tg_to_yt", status="pending", title="Song", artist="A", tg_file_id="f"
    )
    mock_yt_client.get_playlist_tracks = MagicMock(side_effect=_quota_exc())
    sync = TgToYtSync(track_repo, sync_log_repo, mock_tg_client, mock_yt_client)

    assert await sync.process_pending() == 0
    assert await sync.process_pending() == 0
    assert mock_yt_client.get_playlist_tracks.call_count == 1
    mock_yt_client.find_best_match.assert_not_called()


@pytest.mark.asyncio
async def test_same_video_twice_in_one_cycle_is_added_once(
    track_repo: TrackRepository,
    sync_log_repo: SyncLogRepository,
    mock_tg_client: MagicMock,
    mock_yt_client: MagicMock,
) -> None:
    """The playlist snapshot is fetched once per cycle; a bulk post with the same
    song twice must not add the same video to the playlist twice."""
    from navaar.ytmusic.client import YTMusicClient

    for i in range(2):
        await track_repo.create_track(
            direction="tg_to_yt", status="pending", title="Tarikhe Eshgh",
            artist="Hayedeh", tg_file_id=f"file_{i}",
        )
    mock_yt_client.find_best_match = MagicMock(return_value={"videoId": "7FEQ", "title": "x"})
    # Real membership check against the cycle's snapshot.
    mock_yt_client.is_in_playlist = lambda vid, tracks: YTMusicClient.is_in_playlist(
        None, vid, tracks
    )

    with patch("navaar.sync._base_push.identify_track") as mock_identify:
        mock_identify.return_value = MagicMock(
            artist="Hayedeh", title="Tarikhe Eshgh", method="id3"
        )
        sync = TgToYtSync(track_repo, sync_log_repo, mock_tg_client, mock_yt_client)
        await sync.process_pending()

    assert mock_yt_client.add_to_playlist.call_count == 1
    statuses = sorted(t.status for t in await track_repo.get_pending_tracks("tg_to_yt")) or []
    assert statuses == []  # both resolved: one synced, one duplicate
