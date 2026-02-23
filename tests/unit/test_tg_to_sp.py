from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from navaar.db.repository import SyncLogRepository, TrackRepository
from navaar.sync.tg_to_sp import TgToSpSync


@pytest.fixture
def mock_tg_client() -> MagicMock:
    client = MagicMock()
    client.download_file = AsyncMock(return_value="/tmp/test.mp3")
    client.cleanup = MagicMock()
    return client


@pytest.mark.asyncio
async def test_process_pending_no_tracks(
    track_repo: TrackRepository,
    sync_log_repo: SyncLogRepository,
    mock_tg_client: MagicMock,
    mock_sp_client: MagicMock,
) -> None:
    sync = TgToSpSync(track_repo, sync_log_repo, mock_tg_client, mock_sp_client)
    result = await sync.process_pending()
    assert result == 0


@pytest.mark.asyncio
async def test_process_pending_syncs_track(
    track_repo: TrackRepository,
    sync_log_repo: SyncLogRepository,
    mock_tg_client: MagicMock,
    mock_sp_client: MagicMock,
) -> None:
    track = await track_repo.create_track(
        direction="tg_to_sp",
        status="pending",
        title="Hello",
        artist="Adele",
        tg_file_id="file_123",
    )

    with patch("navaar.sync.tg_to_sp.identify_track") as mock_identify:
        mock_identify.return_value = MagicMock(
            artist="Adele", title="Hello", method="tg_metadata"
        )
        sync = TgToSpSync(track_repo, sync_log_repo, mock_tg_client, mock_sp_client)
        result = await sync.process_pending()

    assert result == 1
    updated = await track_repo.get_track(track.id)
    assert updated.status == "synced"
    assert updated.sp_track_id == "sp123"


@pytest.mark.asyncio
async def test_process_pending_no_match(
    track_repo: TrackRepository,
    sync_log_repo: SyncLogRepository,
    mock_tg_client: MagicMock,
    mock_sp_client: MagicMock,
) -> None:
    mock_sp_client.find_best_match = MagicMock(return_value=None)

    await track_repo.create_track(
        direction="tg_to_sp",
        status="pending",
        title="asdfghjkl",
        tg_file_id="file_456",
    )

    with patch("navaar.sync.tg_to_sp.identify_track") as mock_identify:
        mock_identify.return_value = MagicMock(
            artist=None, title="asdfghjkl", method="filename"
        )
        sync = TgToSpSync(track_repo, sync_log_repo, mock_tg_client, mock_sp_client)
        result = await sync.process_pending()

    assert result == 1
    tracks = await track_repo.get_failed_tracks("tg_to_sp")
    assert len(tracks) == 1
    assert tracks[0].failure_reason == "no_sp_match"


@pytest.mark.asyncio
async def test_process_pending_duplicate(
    track_repo: TrackRepository,
    sync_log_repo: SyncLogRepository,
    mock_tg_client: MagicMock,
    mock_sp_client: MagicMock,
) -> None:
    mock_sp_client.is_in_playlist = MagicMock(return_value=True)

    track = await track_repo.create_track(
        direction="tg_to_sp",
        status="pending",
        title="Hello",
        artist="Adele",
        tg_file_id="file_789",
    )

    with patch("navaar.sync.tg_to_sp.identify_track") as mock_identify:
        mock_identify.return_value = MagicMock(
            artist="Adele", title="Hello", method="tg_metadata"
        )
        sync = TgToSpSync(track_repo, sync_log_repo, mock_tg_client, mock_sp_client)
        await sync.process_pending()

    updated = await track_repo.get_track(track.id)
    assert updated.status == "duplicate"
