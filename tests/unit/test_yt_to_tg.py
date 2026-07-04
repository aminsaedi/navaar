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
    # No snapshot at all: first observation must SEED the snapshot and process
    # nothing, not treat the whole pre-existing playlist as new (which would
    # mass-download every song into the channel).
    sync = YtToTgSync(
        track_repo, sync_state_repo, sync_log_repo,
        mock_tg_client, mock_yt_client, mock_downloader,
    )
    result = await sync.process_new_tracks()
    assert result == 0
    mock_downloader.download.assert_not_called()

    # Snapshot is seeded with the current playlist so nothing is re-imported.
    snapshot = await sync_state_repo.get_json("yt_playlist_snapshot")
    assert snapshot == ["vid1", "vid2"]

    # A subsequent cycle with an unchanged playlist still processes nothing.
    assert await sync.process_new_tracks() == 0
    mock_downloader.download.assert_not_called()


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

    # A download failure still creates the DB row, so the id IS recorded in the
    # snapshot (it won't be re-diffed into a duplicate row); recovery is via the
    # Part-1 retry / manual /retry path, not re-discovery.
    snapshot = await sync_state_repo.get_json("yt_playlist_snapshot")
    assert snapshot == ["vid1", "vid2"]


@pytest.mark.asyncio
async def test_pushed_id_is_recorded_and_snapshot_converges(
    track_repo: TrackRepository,
    sync_state_repo: SyncStateRepository,
    sync_log_repo: SyncLogRepository,
    mock_tg_client: MagicMock,
    mock_yt_client: MagicMock,
    mock_downloader: MagicMock,
) -> None:
    # A track pushed onto the YT playlist by tg_to_yt has a synced tg_to_yt row but
    # NO yt_to_tg row. The pull loop must record it in the snapshot (via the skip
    # path) and converge — not re-diff and re-download it every cycle forever.
    await track_repo.create_track(
        direction="tg_to_yt", status="synced", title="Pushed", tg_file_id="ff", yt_video_id="vid1",
    )
    # Snapshot already contains vid2 (so only vid1, the pushed id, is "new").
    await sync_state_repo.set_json("yt_playlist_snapshot", ["vid2"])
    sync = YtToTgSync(
        track_repo, sync_state_repo, sync_log_repo,
        mock_tg_client, mock_yt_client, mock_downloader,
    )
    await sync.process_new_tracks()
    mock_downloader.download.assert_not_called()  # skipped, not re-downloaded

    # vid1 is now recorded, so the snapshot has converged and won't re-diff it.
    snap = await sync_state_repo.get_json("yt_playlist_snapshot")
    assert set(snap) == {"vid1", "vid2"}
    # No spurious yt_to_tg row was created for the pushed track.
    assert not [t for t in await track_repo.get_all_tracks() if t.direction == "yt_to_tg"]

    # Second cycle: vid1 is in the snapshot now, so it is no longer "new" (converged).
    mock_yt_client.get_playlist_tracks.return_value = [
        {"videoId": "vid1", "title": "Pushed", "artists": [{"name": "A"}], "setVideoId": "s1"},
    ]
    assert await sync.process_new_tracks() == 0


@pytest.mark.asyncio
async def test_error_before_row_creation_is_not_snapshotted(
    track_repo: TrackRepository,
    sync_state_repo: SyncStateRepository,
    sync_log_repo: SyncLogRepository,
    mock_tg_client: MagicMock,
    mock_yt_client: MagicMock,
    mock_downloader: MagicMock,
) -> None:
    # vid2 is new. Force _sync_new to raise BEFORE it creates a DB row (the guard
    # lookup errors, e.g. a transient DB error). The id must NOT enter the snapshot,
    # so the next cycle re-diffs and retries it instead of losing the track silently.
    await sync_state_repo.set_json("yt_playlist_snapshot", ["vid1"])
    sync = YtToTgSync(
        track_repo, sync_state_repo, sync_log_repo,
        mock_tg_client, mock_yt_client, mock_downloader,
    )
    original = track_repo.get_track_by_yt_video_id
    track_repo.get_track_by_yt_video_id = AsyncMock(side_effect=RuntimeError("db locked"))
    try:
        assert await sync.process_new_tracks() == 0
    finally:
        track_repo.get_track_by_yt_video_id = original

    # vid2 was not ingested and must be omitted from the snapshot (vid1 preserved).
    assert await sync_state_repo.get_json("yt_playlist_snapshot") == ["vid1"]
    assert await track_repo.get_track_by_yt_video_id("vid2") is None

    # Next cycle (DB healthy again): vid2 is still "new" → gets ingested and synced.
    result = await sync.process_new_tracks()
    assert result == 1
    recovered = await track_repo.get_track_by_yt_video_id("vid2")
    assert recovered is not None and recovered.status == "synced"
    assert await sync_state_repo.get_json("yt_playlist_snapshot") == ["vid1", "vid2"]
