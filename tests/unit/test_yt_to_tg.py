from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from navaar.db.repository import SyncLogRepository, SyncStateRepository, TrackRepository
from navaar.sync.yt_to_tg import YtToTgSync


@pytest.fixture
def mock_tg_client() -> MagicMock:
    client = MagicMock()
    client.send_audio = AsyncMock(side_effect=[42, 43, 44, 45])
    return client


@pytest.fixture
def mock_yt_client() -> MagicMock:
    client = MagicMock()
    client.get_playlist_tracks = MagicMock(
        return_value=[
            {
                "videoId": "vid1",
                "title": "Song One",
                "artists": [{"name": "Artist A"}],
                "duration_seconds": 180,
                "setVideoId": "set1",
            },
            {
                "videoId": "vid2",
                "title": "Song Two",
                "artists": [{"name": "Artist B"}],
                "duration_seconds": 240,
                "setVideoId": "set2",
            },
        ]
    )
    return client


@pytest.fixture
def mock_downloader() -> MagicMock:
    dl = MagicMock()
    dl.download = AsyncMock(return_value="/tmp/vid1.mp3")
    dl.cleanup = MagicMock()
    return dl


@pytest.mark.asyncio
async def test_no_new_tracks(
    track_repo: TrackRepository,
    sync_state_repo: SyncStateRepository,
    sync_log_repo: SyncLogRepository,
    mock_tg_client: MagicMock,
    mock_yt_client: MagicMock,
    mock_downloader: MagicMock,
) -> None:
    # Pre-populate snapshot with all tracks
    await sync_state_repo.set_json("yt_playlist_snapshot", ["vid1", "vid2"])

    sync = YtToTgSync(
        track_repo, sync_state_repo, sync_log_repo,
        mock_tg_client, mock_yt_client, mock_downloader,
    )
    result = await sync.process_new_tracks()
    assert result == 0
    mock_downloader.download.assert_not_called()


@pytest.mark.asyncio
async def test_new_track_synced(
    track_repo: TrackRepository,
    sync_state_repo: SyncStateRepository,
    sync_log_repo: SyncLogRepository,
    mock_tg_client: MagicMock,
    mock_yt_client: MagicMock,
    mock_downloader: MagicMock,
) -> None:
    # Snapshot only has vid1, so vid2 is new
    await sync_state_repo.set_json("yt_playlist_snapshot", ["vid1"])

    sync = YtToTgSync(
        track_repo, sync_state_repo, sync_log_repo,
        mock_tg_client, mock_yt_client, mock_downloader,
    )
    result = await sync.process_new_tracks()
    assert result == 1

    # Verify track was created and synced
    track = await track_repo.get_track_by_yt_video_id("vid2")
    assert track is not None
    assert track.status == "synced"
    assert track.tg_message_id == 42

    mock_downloader.download.assert_called_once_with("vid2")
    mock_tg_client.send_audio.assert_called_once()
    mock_downloader.cleanup.assert_called_once()


@pytest.mark.asyncio
async def test_first_run_empty_snapshot(
    track_repo: TrackRepository,
    sync_state_repo: SyncStateRepository,
    sync_log_repo: SyncLogRepository,
    mock_tg_client: MagicMock,
    mock_yt_client: MagicMock,
    mock_downloader: MagicMock,
) -> None:
    # No snapshot at all — all tracks are "new"
    sync = YtToTgSync(
        track_repo, sync_state_repo, sync_log_repo,
        mock_tg_client, mock_yt_client, mock_downloader,
    )
    result = await sync.process_new_tracks()
    assert result == 2

    # Snapshot should now be saved
    snapshot = await sync_state_repo.get_json("yt_playlist_snapshot")
    assert snapshot == ["vid1", "vid2"]


@pytest.mark.asyncio
async def test_download_failure_marks_failed(
    track_repo: TrackRepository,
    sync_state_repo: SyncStateRepository,
    sync_log_repo: SyncLogRepository,
    mock_tg_client: MagicMock,
    mock_yt_client: MagicMock,
    mock_downloader: MagicMock,
) -> None:
    mock_downloader.download = AsyncMock(side_effect=RuntimeError("yt-dlp failed"))
    await sync_state_repo.set_json("yt_playlist_snapshot", ["vid1"])

    sync = YtToTgSync(
        track_repo, sync_state_repo, sync_log_repo,
        mock_tg_client, mock_yt_client, mock_downloader,
    )
    result = await sync.process_new_tracks()
    # Track creation and attempt still counts, but sync fails
    assert result == 0

    track = await track_repo.get_track_by_yt_video_id("vid2")
    assert track is not None
    assert track.status == "failed"
    assert "download_failed" in track.failure_reason
