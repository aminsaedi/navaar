from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from telegram.error import BadRequest

from navaar.db.repository import TrackRepository
from navaar.telegram.cards import TrackCardService


def _bot(message_id: int = 999) -> MagicMock:
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=message_id))
    bot.edit_message_text = AsyncMock()
    return bot


async def _mk_tg_logical(
    repo: TrackRepository,
    *,
    anchor: int | None = 100,
    yt: tuple[str, str] | None = None,   # (status, yt_video_id)
    sp: tuple[str, str] | None = None,   # (status, sp_track_id)
):
    """Create a TG-origin logical track (tg_to_yt primary + tg_to_sp fan-out),
    sharing tg_file_id. Returns the primary row."""
    primary = await repo.create_track(
        direction="tg_to_yt",
        status=(yt[0] if yt else "pending"),
        title="Bohemian Rhapsody",
        artist="Queen",
        tg_file_id="FID",
        tg_file_unique_id="UID",
        tg_message_id=anchor,
        yt_video_id=(yt[1] if yt else None),
    )
    await repo.create_track(
        direction="tg_to_sp",
        status=(sp[0] if sp else "pending"),
        title="Bohemian Rhapsody",
        artist="Queen",
        tg_file_id="FID",
        sp_track_id=(sp[1] if sp else None),
    )
    return primary


# ── Repository correlation ───────────────────────────────────────────


async def test_get_sibling_tracks_correlates_by_origin_id(track_repo: TrackRepository) -> None:
    primary = await _mk_tg_logical(track_repo)
    siblings = await track_repo.get_sibling_tracks(primary)
    assert {s.direction for s in siblings} == {"tg_to_yt", "tg_to_sp"}


async def test_set_card_message_id_stamps_all_siblings(track_repo: TrackRepository) -> None:
    primary = await _mk_tg_logical(track_repo)
    siblings = await track_repo.get_sibling_tracks(primary)
    await track_repo.set_card_message_id([s.id for s in siblings], 555)
    for s in siblings:
        refreshed = await track_repo.get_track(s.id)
        assert refreshed.card_message_id == 555


async def test_get_sibling_tracks_without_origin_id_returns_self(track_repo: TrackRepository) -> None:
    # A yt_to_tg row before download has no yt_video_id-less... it always has one;
    # use a contrived row with no correlating id.
    lone = await track_repo.create_track(
        direction="yt_to_tg", status="pending", title="x", artist="y"
    )
    siblings = await track_repo.get_sibling_tracks(lone)
    assert [s.id for s in siblings] == [lone.id]


# ── First post / edit lifecycle ──────────────────────────────────────


async def test_refresh_posts_card_and_stamps_siblings(track_repo: TrackRepository) -> None:
    primary = await _mk_tg_logical(track_repo, anchor=100)
    bot = _bot(message_id=777)
    svc = TrackCardService(bot, -1003744100092, track_repo, sp_enabled=True)

    await svc.refresh(primary.id)

    bot.send_message.assert_awaited_once()
    kwargs = bot.send_message.call_args.kwargs
    assert kwargs["reply_to_message_id"] == 100
    assert kwargs["chat_id"] == -1003744100092
    bot.edit_message_text.assert_not_called()

    for s in await track_repo.get_sibling_tracks(primary):
        assert s.card_message_id == 777


async def test_refresh_edits_existing_card(track_repo: TrackRepository) -> None:
    primary = await _mk_tg_logical(track_repo, anchor=100)
    siblings = await track_repo.get_sibling_tracks(primary)
    await track_repo.set_card_message_id([s.id for s in siblings], 777)

    bot = _bot()
    svc = TrackCardService(bot, -1003744100092, track_repo, sp_enabled=True)
    await svc.refresh(primary.id)

    bot.send_message.assert_not_called()
    bot.edit_message_text.assert_awaited_once()
    assert bot.edit_message_text.call_args.kwargs["message_id"] == 777


async def test_refresh_skips_when_no_anchor(track_repo: TrackRepository) -> None:
    # YT-origin track not yet uploaded to TG → no tg_message_id anywhere → no card.
    primary = await track_repo.create_track(
        direction="yt_to_tg", status="pending", title="t", artist="a",
        yt_video_id="VID",
    )
    await track_repo.create_track(
        direction="yt_to_sp", status="pending", title="t", artist="a", yt_video_id="VID",
    )
    bot = _bot()
    svc = TrackCardService(bot, -1003744100092, track_repo, sp_enabled=True)
    await svc.refresh(primary.id)

    bot.send_message.assert_not_called()
    bot.edit_message_text.assert_not_called()


# ── Error handling ───────────────────────────────────────────────────


async def test_refresh_ignores_not_modified(track_repo: TrackRepository) -> None:
    primary = await _mk_tg_logical(track_repo, anchor=100)
    siblings = await track_repo.get_sibling_tracks(primary)
    await track_repo.set_card_message_id([s.id for s in siblings], 777)

    bot = _bot()
    bot.edit_message_text = AsyncMock(side_effect=BadRequest("Message is not modified"))
    svc = TrackCardService(bot, -1003744100092, track_repo, sp_enabled=True)

    # Must not raise.
    await svc.refresh(primary.id)


async def test_refresh_swallows_unexpected_errors(track_repo: TrackRepository) -> None:
    primary = await _mk_tg_logical(track_repo, anchor=100)
    bot = _bot()
    bot.send_message = AsyncMock(side_effect=RuntimeError("telegram exploded"))
    svc = TrackCardService(bot, -1003744100092, track_repo, sp_enabled=True)

    # Best-effort: the failure must be swallowed, never propagated to the sync loop.
    await svc.refresh(primary.id)


async def test_disabled_service_is_noop(track_repo: TrackRepository) -> None:
    primary = await _mk_tg_logical(track_repo, anchor=100)
    bot = _bot()
    svc = TrackCardService(bot, -1003744100092, track_repo, sp_enabled=True, enabled=False)
    await svc.refresh(primary.id)
    bot.send_message.assert_not_called()


# ── Rendering ────────────────────────────────────────────────────────


async def test_render_marks_origin_and_target_statuses(track_repo: TrackRepository) -> None:
    primary = await _mk_tg_logical(
        track_repo, yt=("synced", "YTVID"), sp=("pending", None)
    )
    svc = TrackCardService(_bot(), -1003744100092, track_repo, sp_enabled=True)
    siblings = await track_repo.get_sibling_tracks(primary)
    text, keyboard = svc._render(siblings, "tg")

    assert "Queen — Bohemian Rhapsody" in text
    assert "First seen on Telegram" in text
    assert "Telegram · <i>source</i>" in text
    assert "✅ synced" in text       # YouTube target
    assert "⏳ pending" in text       # Spotify target

    # Only the synced YT target produces a button.
    urls = [b.url for row in keyboard.inline_keyboard for b in row]
    assert "https://music.youtube.com/watch?v=YTVID" in urls
    assert all("open.spotify.com" not in u for u in urls)


async def test_render_builds_both_links_when_synced(track_repo: TrackRepository) -> None:
    primary = await _mk_tg_logical(
        track_repo, yt=("synced", "YTVID"), sp=("synced", "SPID")
    )
    svc = TrackCardService(_bot(), -1003744100092, track_repo, sp_enabled=True)
    siblings = await track_repo.get_sibling_tracks(primary)
    _text, keyboard = svc._render(siblings, "tg")

    urls = {b.url for row in keyboard.inline_keyboard for b in row}
    assert "https://music.youtube.com/watch?v=YTVID" in urls
    assert "https://open.spotify.com/track/SPID" in urls


def test_tme_link_for_private_channel(track_repo: TrackRepository) -> None:
    svc = TrackCardService(_bot(), -1003744100092, track_repo, sp_enabled=True)
    assert svc._tme_link(630) == "https://t.me/c/3744100092/630"
