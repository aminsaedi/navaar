from __future__ import annotations

import pytest

from navaar.db.repository import SyncStateRepository, TrackRepository


@pytest.mark.asyncio
async def test_create_and_get_track(track_repo: TrackRepository) -> None:
    track = await track_repo.create_track(
        direction="tg_to_yt",
        status="pending",
        title="Hello",
        artist="Adele",
        tg_message_id=1,
        tg_file_id="file_123",
        tg_file_unique_id="unique_123",
    )
    assert track.id is not None
    assert track.title == "Hello"
    assert track.status == "pending"

    fetched = await track_repo.get_track(track.id)
    assert fetched is not None
    assert fetched.title == "Hello"


@pytest.mark.asyncio
async def test_get_track_by_tg_file_unique_id(track_repo: TrackRepository) -> None:
    await track_repo.create_track(
        direction="tg_to_yt",
        status="pending",
        title="Test",
        tg_file_unique_id="abc123",
    )
    found = await track_repo.get_track_by_tg_file_unique_id("abc123")
    assert found is not None
    assert found.title == "Test"

    not_found = await track_repo.get_track_by_tg_file_unique_id("xyz")
    assert not_found is None


@pytest.mark.asyncio
async def test_get_track_by_yt_video_id(track_repo: TrackRepository) -> None:
    await track_repo.create_track(
        direction="yt_to_tg",
        status="synced",
        title="Song",
        yt_video_id="dQw4w9WgXcQ",
    )
    found = await track_repo.get_track_by_yt_video_id("dQw4w9WgXcQ")
    assert found is not None
    assert found.title == "Song"


@pytest.mark.asyncio
async def test_get_pending_tracks(track_repo: TrackRepository) -> None:
    await track_repo.create_track(direction="tg_to_yt", status="pending", title="A")
    await track_repo.create_track(direction="tg_to_yt", status="synced", title="B")
    await track_repo.create_track(direction="tg_to_yt", status="retry_scheduled", title="C")
    await track_repo.create_track(direction="yt_to_tg", status="pending", title="D")

    pending = await track_repo.get_pending_tracks("tg_to_yt")
    assert len(pending) == 2
    titles = {t.title for t in pending}
    assert titles == {"A", "C"}


@pytest.mark.asyncio
async def test_mark_synced(track_repo: TrackRepository) -> None:
    track = await track_repo.create_track(direction="tg_to_yt", status="pending", title="X")
    updated = await track_repo.mark_synced(track.id, yt_video_id="vid123")
    assert updated.status == "synced"
    assert updated.synced_at is not None
    assert updated.yt_video_id == "vid123"


@pytest.mark.asyncio
async def test_mark_failed_and_retry(track_repo: TrackRepository) -> None:
    track = await track_repo.create_track(direction="tg_to_yt", status="pending", title="X")
    failed = await track_repo.mark_failed(track.id, "no_yt_match")
    assert failed.status == "failed"
    assert failed.failure_reason == "no_yt_match"
    assert failed.retry_count == 1

    retried = await track_repo.reset_for_retry(track.id)
    assert retried.status == "retry_scheduled"
    assert retried.failure_reason is None


@pytest.mark.asyncio
async def test_mark_duplicate(track_repo: TrackRepository) -> None:
    track = await track_repo.create_track(direction="tg_to_yt", status="pending", title="X")
    updated = await track_repo.mark_duplicate(track.id)
    assert updated.status == "duplicate"


@pytest.mark.asyncio
async def test_get_failed_tracks(track_repo: TrackRepository) -> None:
    await track_repo.create_track(direction="tg_to_yt", status="failed", title="A")
    await track_repo.create_track(direction="yt_to_tg", status="failed", title="B")
    await track_repo.create_track(direction="tg_to_yt", status="synced", title="C")

    all_failed = await track_repo.get_failed_tracks()
    assert len(all_failed) == 2

    tg_failed = await track_repo.get_failed_tracks("tg_to_yt")
    assert len(tg_failed) == 1
    assert tg_failed[0].title == "A"


@pytest.mark.asyncio
async def test_reset_all_failed(track_repo: TrackRepository) -> None:
    await track_repo.create_track(direction="tg_to_yt", status="failed", title="A")
    await track_repo.create_track(direction="tg_to_yt", status="failed", title="B")
    await track_repo.create_track(direction="yt_to_tg", status="failed", title="C")

    count = await track_repo.reset_all_failed("tg_to_yt")
    assert count == 2

    remaining = await track_repo.get_failed_tracks()
    assert len(remaining) == 1
    assert remaining[0].title == "C"


@pytest.mark.asyncio
async def test_get_counts(track_repo: TrackRepository) -> None:
    await track_repo.create_track(direction="tg_to_yt", status="synced", title="A")
    await track_repo.create_track(direction="tg_to_yt", status="synced", title="B")
    await track_repo.create_track(direction="tg_to_yt", status="failed", title="C")
    await track_repo.create_track(direction="yt_to_tg", status="pending", title="D")

    counts = await track_repo.get_counts()
    assert counts["tg_to_yt"]["synced"] == 2
    assert counts["tg_to_yt"]["failed"] == 1
    assert counts["yt_to_tg"]["pending"] == 1


@pytest.mark.asyncio
async def test_get_stats(track_repo: TrackRepository) -> None:
    await track_repo.create_track(direction="tg_to_yt", status="synced", title="A")
    await track_repo.create_track(direction="tg_to_yt", status="failed", title="B")
    await track_repo.create_track(direction="tg_to_yt", status="duplicate", title="C")

    stats = await track_repo.get_stats()
    assert stats["total"] == 3
    assert stats["synced"] == 1
    assert stats["failed"] == 1
    assert stats["duplicates"] == 1
    assert stats["success_rate"] == 33.3


@pytest.mark.asyncio
async def test_sync_state_crud(sync_state_repo: SyncStateRepository) -> None:
    assert await sync_state_repo.get("key1") is None

    await sync_state_repo.set("key1", "value1")
    assert await sync_state_repo.get("key1") == "value1"

    await sync_state_repo.set("key1", "value2")
    assert await sync_state_repo.get("key1") == "value2"


@pytest.mark.asyncio
async def test_sync_state_json(sync_state_repo: SyncStateRepository) -> None:
    await sync_state_repo.set_json("snapshot", ["vid1", "vid2"])
    result = await sync_state_repo.get_json("snapshot")
    assert result == ["vid1", "vid2"]
