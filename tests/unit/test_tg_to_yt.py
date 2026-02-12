from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from navaar.db.repository import SyncLogRepository, TrackRepository
from navaar.sync.tg_to_yt import TgToYtSync


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

    with patch("navaar.sync.tg_to_yt.identify_track") as mock_identify:
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

    with patch("navaar.sync.tg_to_yt.identify_track") as mock_identify:
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

    with patch("navaar.sync.tg_to_yt.identify_track") as mock_identify:
        mock_identify.return_value = MagicMock(
            artist="Adele", title="Hello", method="tg_metadata"
        )
        sync = TgToYtSync(track_repo, sync_log_repo, mock_tg_client, mock_yt_client)
        await sync.process_pending()

    updated = await track_repo.get_track(track.id)
    assert updated.status == "duplicate"
