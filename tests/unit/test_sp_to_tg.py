from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from navaar.db.repository import SyncLogRepository, SyncStateRepository, TrackRepository
from navaar.sync.sp_to_tg import SpToTgSync


@pytest.fixture
def mock_tg_client() -> MagicMock:
    client = MagicMock()
    client.send_audio = AsyncMock(side_effect=[42, 43, 44, 45])
    return client


@pytest.fixture
def mock_yt_client() -> MagicMock:
    client = MagicMock()
    client.find_best_match = MagicMock(
        return_value={"videoId": "yt_vid1", "title": "Song One"}
    )
    return client


@pytest.fixture
def mock_downloader() -> MagicMock:
    dl = MagicMock()
    dl.download = AsyncMock(return_value="/tmp/vid1.mp3")
    dl.cleanup = MagicMock()
    return dl


def _make_sp_client(tracks: list[dict]) -> MagicMock:
    client = MagicMock()
    client.get_playlist_tracks = MagicMock(return_value=tracks)
    return client


@pytest.mark.asyncio
async def test_no_new_tracks(
    track_repo: TrackRepository,
    sync_state_repo: SyncStateRepository,
    sync_log_repo: SyncLogRepository,
    mock_tg_client: MagicMock,
    mock_yt_client: MagicMock,
    mock_downloader: MagicMock,
) -> None:
    sp_tracks = [
        {"id": "sp1", "name": "Song One", "artists": ["Artist A"], "duration_ms": 180000, "uri": "spotify:track:sp1"},
        {"id": "sp2", "name": "Song Two", "artists": ["Artist B"], "duration_ms": 240000, "uri": "spotify:track:sp2"},
    ]
    sp_client = _make_sp_client(sp_tracks)
    await sync_state_repo.set_json("sp_playlist_snapshot", ["sp1", "sp2"])

    sync = SpToTgSync(
        track_repo, sync_state_repo, sync_log_repo,
        mock_tg_client, sp_client, mock_yt_client, mock_downloader,
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
    sp_tracks = [
        {"id": "sp1", "name": "Song One", "artists": ["Artist A"], "duration_ms": 180000, "uri": "spotify:track:sp1"},
        {"id": "sp2", "name": "Song Two", "artists": ["Artist B"], "duration_ms": 240000, "uri": "spotify:track:sp2"},
    ]
    sp_client = _make_sp_client(sp_tracks)
    # Snapshot only has sp1, so sp2 is new
    await sync_state_repo.set_json("sp_playlist_snapshot", ["sp1"])

    sync = SpToTgSync(
        track_repo, sync_state_repo, sync_log_repo,
        mock_tg_client, sp_client, mock_yt_client, mock_downloader,
    )
    result = await sync.process_new_tracks()
    assert result == 1

    # Verify sp_to_tg track was created and synced
    track = await track_repo.get_track_by_sp_track_id("sp2")
    assert track is not None
    assert track.status == "synced"
    assert track.tg_message_id == 42

    mock_downloader.download.assert_called_once_with("yt_vid1")
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
    sp_tracks = [
        {"id": "sp1", "name": "Song One", "artists": ["Artist A"], "duration_ms": 180000, "uri": "spotify:track:sp1"},
        {"id": "sp2", "name": "Song Two", "artists": ["Artist B"], "duration_ms": 240000, "uri": "spotify:track:sp2"},
    ]
    sp_client = _make_sp_client(sp_tracks)
    # No snapshot at all
    sync = SpToTgSync(
        track_repo, sync_state_repo, sync_log_repo,
        mock_tg_client, sp_client, mock_yt_client, mock_downloader,
    )
    result = await sync.process_new_tracks()
    assert result == 2

    # Snapshot should now be saved
    snapshot = await sync_state_repo.get_json("sp_playlist_snapshot")
    assert snapshot == ["sp1", "sp2"]


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
    sp_tracks = [
        {"id": "sp1", "name": "Song One", "artists": ["Artist A"], "duration_ms": 180000, "uri": "spotify:track:sp1"},
        {"id": "sp2", "name": "Song Two", "artists": ["Artist B"], "duration_ms": 240000, "uri": "spotify:track:sp2"},
    ]
    sp_client = _make_sp_client(sp_tracks)
    await sync_state_repo.set_json("sp_playlist_snapshot", ["sp1"])

    sync = SpToTgSync(
        track_repo, sync_state_repo, sync_log_repo,
        mock_tg_client, sp_client, mock_yt_client, mock_downloader,
    )
    result = await sync.process_new_tracks()
    assert result == 0

    track = await track_repo.get_track_by_sp_track_id("sp2")
    assert track is not None
    assert track.status == "failed"
    assert "download_failed" in track.failure_reason
