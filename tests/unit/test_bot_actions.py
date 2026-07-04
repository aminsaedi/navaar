from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from navaar.db.repository import SyncLogRepository, TrackRepository
from navaar.metrics import RETRIES_TOTAL
from navaar.telegram.bot import NavaarBot


def _bot(track_repo, sync_log, *, yt=None, sp=None, engine=None) -> NavaarBot:
    bot = NavaarBot(
        token="x",
        channel_id=-1001234567890,
        admin_user_ids=[111],
        track_repo=track_repo,
        sync_log=sync_log,
        yt_client=yt,
        sp_client=sp,
    )
    bot._app = MagicMock()
    bot._app.bot = MagicMock()
    bot._app.bot.delete_message = AsyncMock()
    if engine is not None:
        bot._engine = engine
    return bot


def _admin_update() -> MagicMock:
    msg = MagicMock(reply_text=AsyncMock())
    return MagicMock(message=msg, effective_user=MagicMock(id=111), callback_query=None)


def _reply_text(update: MagicMock) -> str:
    return update.message.reply_text.await_args.args[0]


@pytest.fixture
async def sync_log(session_factory) -> SyncLogRepository:
    return SyncLogRepository(session_factory)


# ── /retry all metric (the 6x over-count fix) ────────────────────────


async def test_reset_all_failed_metered_is_per_direction(
    track_repo: TrackRepository, sync_log: SyncLogRepository
) -> None:
    # 2 failed tg_to_yt + 1 failed yt_to_tg. The metric must gain exactly the real
    # per-direction counts, NOT the global total times six.
    for _ in range(2):
        await track_repo.create_track(direction="tg_to_yt", status="failed", title="a")
    await track_repo.create_track(direction="yt_to_tg", status="failed", title="b")

    def val(d: str) -> float:
        return RETRIES_TOTAL.labels(direction=d)._value.get()

    before = {d: val(d) for d in ("tg_to_yt", "yt_to_tg", "tg_to_sp", "sp_to_tg", "yt_to_sp", "sp_to_yt")}
    bot = _bot(track_repo, sync_log)
    total = await bot._reset_all_failed_metered()

    assert total == 3
    assert val("tg_to_yt") - before["tg_to_yt"] == 2
    assert val("yt_to_tg") - before["yt_to_tg"] == 1
    # Directions with no failures must not be incremented at all (no misattribution).
    for d in ("tg_to_sp", "sp_to_tg", "yt_to_sp", "sp_to_yt"):
        assert val(d) - before[d] == 0

    # And the rows are actually reset for retry.
    assert not await track_repo.get_failed_tracks()


# ── /delete → full logical purge ─────────────────────────────────────


async def test_purge_logical_track_removes_playlists_messages_and_rows(
    track_repo: TrackRepository, sync_log: SyncLogRepository
) -> None:
    # One logical track (shared tg_file_id) synced to both YT and SP.
    t1 = await track_repo.create_track(
        direction="tg_to_yt", status="synced", title="Song", artist="X",
        tg_file_id="f1", tg_message_id=500, card_message_id=501, yt_video_id="vidZ",
    )
    await track_repo.create_track(
        direction="tg_to_sp", status="synced", title="Song", artist="X",
        tg_file_id="f1", sp_track_id="spZ",
    )
    yt = MagicMock(remove_from_playlist=MagicMock())
    sp = MagicMock(remove_from_playlist=MagicMock())
    bot = _bot(track_repo, sync_log, yt=yt, sp=sp)

    summary = await bot._purge_logical_track(t1.id)

    yt.remove_from_playlist.assert_called_once_with("vidZ")
    sp.remove_from_playlist.assert_called_once_with("spZ")
    # Both the audio message and the status card are deleted.
    deleted_ids = {c.kwargs["message_id"] for c in bot._app.bot.delete_message.await_args_list}
    assert deleted_ids == {500, 501}
    # Every sibling row is gone.
    assert await track_repo.get_track(t1.id) is None
    assert await track_repo.get_track_by_sp_track_id("spZ") is None
    assert "Deleted" in summary and "removed from" in summary


async def test_purge_logical_track_not_found(
    track_repo: TrackRepository, sync_log: SyncLogRepository
) -> None:
    bot = _bot(track_repo, sync_log)
    assert "not found" in await bot._purge_logical_track(9999)


async def test_purge_summary_does_not_claim_removal_when_not_in_playlist(
    track_repo: TrackRepository, sync_log: SyncLogRepository
) -> None:
    # YT returns False when the id wasn't actually in the playlist. The summary must
    # NOT claim "removed from YT" then (no false success), but rows are still deleted.
    t = await track_repo.create_track(
        direction="tg_to_yt", status="synced", title="S", tg_file_id="fx", yt_video_id="vx",
    )
    yt = MagicMock(remove_from_playlist=MagicMock(return_value=False))
    bot = _bot(track_repo, sync_log, yt=yt)
    summary = await bot._purge_logical_track(t.id)
    assert "removed from" not in summary
    assert await track_repo.get_track(t.id) is None


async def test_purge_survives_playlist_removal_error(
    track_repo: TrackRepository, sync_log: SyncLogRepository
) -> None:
    # A failing playlist-removal must not block deleting the DB rows.
    t = await track_repo.create_track(
        direction="tg_to_yt", status="synced", title="S", tg_file_id="f9", yt_video_id="v9",
    )
    yt = MagicMock(remove_from_playlist=MagicMock(side_effect=RuntimeError("api down")))
    bot = _bot(track_repo, sync_log, yt=yt)
    await bot._purge_logical_track(t.id)
    assert await track_repo.get_track(t.id) is None


# ── /health (partial-sync report) ────────────────────────────────────


async def test_health_reports_partial_and_hides_complete(
    track_repo: TrackRepository, sync_log: SyncLogRepository
) -> None:
    # Logical A: synced to YT, FAILED on SP -> partial (should appear).
    await track_repo.create_track(
        direction="tg_to_yt", status="synced", title="Partial", tg_file_id="fa", yt_video_id="va",
    )
    await track_repo.create_track(
        direction="tg_to_sp", status="failed", title="Partial", tg_file_id="fa",
        failure_reason="no_sp_match",
    )
    # Logical B: synced everywhere -> complete (should NOT appear).
    await track_repo.create_track(
        direction="tg_to_yt", status="synced", title="Complete", tg_file_id="fb", yt_video_id="vb",
    )
    await track_repo.create_track(
        direction="tg_to_sp", status="synced", title="Complete", tg_file_id="fb", sp_track_id="sb",
    )
    bot = _bot(track_repo, sync_log, sp=MagicMock())
    upd = _admin_update()
    await bot._cmd_health(upd, MagicMock(args=[]))
    text = _reply_text(upd)
    assert "Partial" in text
    assert "Complete" not in text


async def test_health_all_synced_says_healthy(
    track_repo: TrackRepository, sync_log: SyncLogRepository
) -> None:
    await track_repo.create_track(
        direction="tg_to_yt", status="synced", title="OK", tg_file_id="fc", yt_video_id="vc",
    )
    bot = _bot(track_repo, sync_log)
    upd = _admin_update()
    await bot._cmd_health(upd, MagicMock(args=[]))
    assert "every tracked song" in _reply_text(upd)


# ── /digest ──────────────────────────────────────────────────────────


async def test_digest_lists_recent_logical_tracks(
    track_repo: TrackRepository, sync_log: SyncLogRepository
) -> None:
    await track_repo.create_track(
        direction="tg_to_yt", status="synced", title="Fresh", tg_file_id="fd", yt_video_id="vd",
    )
    bot = _bot(track_repo, sync_log)
    upd = _admin_update()
    await bot._cmd_digest(upd, MagicMock(args=["7"]))
    text = _reply_text(upd)
    assert "Fresh" in text and "Digest" in text


async def test_digest_empty_window(
    track_repo: TrackRepository, sync_log: SyncLogRepository
) -> None:
    # A track created "8 days ago" is outside a 1-day window.
    t = await track_repo.create_track(direction="tg_to_yt", status="synced", title="Old", tg_file_id="fe")
    await track_repo.update_track(t.id, created_at=datetime.now(UTC) - timedelta(days=8))
    bot = _bot(track_repo, sync_log)
    upd = _admin_update()
    await bot._cmd_digest(upd, MagicMock(args=["1"]))
    assert "Nothing added" in _reply_text(upd)


# ── /link ─────────────────────────────────────────────────────────────


async def test_link_builds_platform_buttons(
    track_repo: TrackRepository, sync_log: SyncLogRepository
) -> None:
    t = await track_repo.create_track(
        direction="tg_to_yt", status="synced", title="L", tg_file_id="fl",
        tg_message_id=700, yt_video_id="vl",
    )
    await track_repo.create_track(
        direction="tg_to_sp", status="synced", title="L", tg_file_id="fl", sp_track_id="sl",
    )
    bot = _bot(track_repo, sync_log, sp=MagicMock())
    upd = _admin_update()
    await bot._cmd_link(upd, MagicMock(args=[str(t.id)]))
    kb = upd.message.reply_text.await_args.kwargs["reply_markup"]
    urls = [b.url for row in kb.inline_keyboard for b in row]
    assert any("music.youtube.com/watch?v=vl" in u for u in urls)
    assert any("open.spotify.com/track/sl" in u for u in urls)
    assert any("t.me/c/1234567890/700" in u for u in urls)


# ── search-and-add callback ──────────────────────────────────────────


async def test_add_yt_callback_adds_and_forces_sync(
    track_repo: TrackRepository, sync_log: SyncLogRepository
) -> None:
    yt = MagicMock(add_to_playlist=MagicMock())
    engine = MagicMock(force_sync=MagicMock())
    bot = _bot(track_repo, sync_log, yt=yt, engine=engine)
    query = MagicMock(message=MagicMock(reply_text=AsyncMock()))
    await bot._handle_add_callback(query, "add_yt_myvideoid")
    yt.add_to_playlist.assert_called_once_with("myvideoid")
    engine.force_sync.assert_called_once_with("yt_to_tg")
    query.message.reply_text.assert_awaited_once()


async def test_add_sp_callback_adds_and_forces_sync(
    track_repo: TrackRepository, sync_log: SyncLogRepository
) -> None:
    sp = MagicMock(add_to_playlist=MagicMock())
    engine = MagicMock(force_sync=MagicMock())
    bot = _bot(track_repo, sync_log, sp=sp, engine=engine)
    query = MagicMock(message=MagicMock(reply_text=AsyncMock()))
    await bot._handle_add_callback(query, "add_sp_sometrackid")
    sp.add_to_playlist.assert_called_once_with("sometrackid")
    engine.force_sync.assert_called_once_with("sp_to_tg")
