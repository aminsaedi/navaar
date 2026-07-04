from __future__ import annotations

import pytest

from navaar.db.repository import TrackRepository
from navaar.sync.fanout import FanOut


async def test_from_telegram_creates_sp_row_and_dedups(track_repo: TrackRepository) -> None:
    fo = FanOut(track_repo, sp_enabled=True)
    await fo.from_telegram(tg_file_id="f1", title="T", artist="A", duration=100)
    assert await track_repo.has_track_for_direction("tg_to_sp", tg_file_id="f1")
    # A second fan-out for the same file must be a no-op (no duplicate/loop row).
    await fo.from_telegram(tg_file_id="f1", title="T", artist="A", duration=100)
    rows = [t for t in await track_repo.get_all_tracks() if t.direction == "tg_to_sp"]
    assert len(rows) == 1


async def test_from_telegram_noop_when_spotify_disabled(track_repo: TrackRepository) -> None:
    fo = FanOut(track_repo, sp_enabled=False)
    await fo.from_telegram(tg_file_id="f2", title="T", artist="A", duration=100)
    assert not await track_repo.has_track_for_direction("tg_to_sp", tg_file_id="f2")


async def test_from_youtube_creates_yt_to_sp_and_dedups(track_repo: TrackRepository) -> None:
    fo = FanOut(track_repo, sp_enabled=True)
    await fo.from_youtube(video_id="v1", title="T", artist="A", duration=100)
    await fo.from_youtube(video_id="v1", title="T", artist="A", duration=100)
    rows = [t for t in await track_repo.get_all_tracks() if t.direction == "yt_to_sp"]
    assert len(rows) == 1
    assert rows[0].yt_video_id == "v1"


async def test_from_youtube_noop_when_spotify_disabled(track_repo: TrackRepository) -> None:
    fo = FanOut(track_repo, sp_enabled=False)
    await fo.from_youtube(video_id="v2", title="T", artist="A", duration=100)
    assert not await track_repo.has_track_for_direction("yt_to_sp", yt_video_id="v2")


async def test_from_spotify_creates_sp_to_yt_and_dedups(track_repo: TrackRepository) -> None:
    fo = FanOut(track_repo, sp_enabled=True)
    await fo.from_spotify(sp_track_id="s1", title="T", artist="A", duration=100)
    await fo.from_spotify(sp_track_id="s1", title="T", artist="A", duration=100)
    rows = [t for t in await track_repo.get_all_tracks() if t.direction == "sp_to_yt"]
    assert len(rows) == 1
    assert rows[0].sp_track_id == "s1"


@pytest.mark.parametrize(
    "kwargs,direction",
    [
        ({"yt_video_id": "x"}, "yt_to_sp"),
        ({"sp_track_id": "y"}, "sp_to_yt"),
        ({"tg_file_id": "z"}, "tg_to_sp"),
    ],
)
async def test_has_track_for_direction_matches_id_and_direction(
    track_repo: TrackRepository, kwargs: dict, direction: str
) -> None:
    await track_repo.create_track(direction=direction, status="pending", title="t", **kwargs)
    assert await track_repo.has_track_for_direction(direction, **kwargs)
    # Same id, different direction -> no match.
    assert not await track_repo.has_track_for_direction("tg_to_yt", **kwargs)
