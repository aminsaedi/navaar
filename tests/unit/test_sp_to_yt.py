from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from navaar.db.repository import SyncLogRepository, TrackRepository
from navaar.sync.sp_to_yt import SpToYtSync


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
    mock_sp_client: MagicMock,
    mock_yt_client: MagicMock,
) -> None:
    sync = SpToYtSync(track_repo, sync_log_repo, mock_sp_client, mock_yt_client)
    result = await sync.process_pending()
    assert result == 0


@pytest.mark.asyncio
async def test_process_pending_syncs_track(
    track_repo: TrackRepository,
    sync_log_repo: SyncLogRepository,
    mock_sp_client: MagicMock,
    mock_yt_client: MagicMock,
) -> None:
    track = await track_repo.create_track(
        direction="sp_to_yt",
        status="pending",
        title="Hello",
        artist="Adele",
        sp_track_id="sp123",
    )

    sync = SpToYtSync(track_repo, sync_log_repo, mock_sp_client, mock_yt_client)
    result = await sync.process_pending()

    assert result == 1
    updated = await track_repo.get_track(track.id)
    assert updated.status == "synced"
    assert updated.yt_video_id == "abc123"


@pytest.mark.asyncio
async def test_process_pending_no_match(
    track_repo: TrackRepository,
    sync_log_repo: SyncLogRepository,
    mock_sp_client: MagicMock,
    mock_yt_client: MagicMock,
) -> None:
    mock_yt_client.find_best_match = MagicMock(return_value=None)

    await track_repo.create_track(
        direction="sp_to_yt",
        status="pending",
        title="asdfghjkl",
        sp_track_id="sp456",
    )

    sync = SpToYtSync(track_repo, sync_log_repo, mock_sp_client, mock_yt_client)
    result = await sync.process_pending()

    assert result == 1
    tracks = await track_repo.get_failed_tracks("sp_to_yt")
    assert len(tracks) == 1
    assert tracks[0].failure_reason == "no_yt_match"


@pytest.mark.asyncio
async def test_process_pending_duplicate(
    track_repo: TrackRepository,
    sync_log_repo: SyncLogRepository,
    mock_sp_client: MagicMock,
    mock_yt_client: MagicMock,
) -> None:
    mock_yt_client.is_in_playlist = MagicMock(return_value=True)

    track = await track_repo.create_track(
        direction="sp_to_yt",
        status="pending",
        title="Hello",
        artist="Adele",
        sp_track_id="sp789",
    )

    sync = SpToYtSync(track_repo, sync_log_repo, mock_sp_client, mock_yt_client)
    await sync.process_pending()

    updated = await track_repo.get_track(track.id)
    assert updated.status == "duplicate"
