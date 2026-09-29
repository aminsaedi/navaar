from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram import ReactionCount, ReactionTypeCustomEmoji, ReactionTypeEmoji

from navaar.db.repository import SyncLogRepository, TrackRepository
from navaar.sync.tg_to_yt import TgToYtSync
from navaar.telegram.bot import NavaarBot

CHANNEL = -1001234567890
DOWN = "\U0001f44e"


@pytest.fixture
async def sync_log(session_factory) -> SyncLogRepository:
    return SyncLogRepository(session_factory)


def _bot(track_repo, sync_log, *, threshold=2, yt=None, sp=None) -> NavaarBot:
    bot = NavaarBot(
        token="x",
        channel_id=CHANNEL,
        admin_user_ids=[111],
        track_repo=track_repo,
        sync_log=sync_log,
        yt_client=yt or MagicMock(remove_from_playlist=MagicMock(return_value=True)),
        sp_client=sp or MagicMock(remove_from_playlist=MagicMock(return_value=None)),
        dislike_threshold=threshold,
    )
    bot._app = MagicMock()
    bot._app.bot.delete_message = AsyncMock()
    bot._app.bot.send_message = AsyncMock()
    return bot


def _update(message_id: int, reactions: list, chat_id: int = CHANNEL) -> MagicMock:
    upd = MagicMock()
    upd.message_reaction_count = MagicMock(
        chat=MagicMock(id=chat_id), message_id=message_id, reactions=reactions
    )
    return upd


def _rc(emoji: str, n: int) -> ReactionCount:
    return ReactionCount(type=ReactionTypeEmoji(emoji), total_count=n)


async def _logical_track(track_repo: TrackRepository):
    t1 = await track_repo.create_track(
        direction="tg_to_yt", status="synced", title="Song", artist="X",
        tg_file_id="f1", tg_file_unique_id="u1", tg_message_id=500, card_message_id=501,
        yt_video_id="vidZ",
    )
    await track_repo.create_track(
        direction="tg_to_sp", status="synced", title="Song", artist="X",
        tg_file_id="f1", card_message_id=501, sp_track_id="spZ",
    )
    return t1


async def test_two_dislikes_remove_everywhere_and_announce(track_repo, sync_log) -> None:
    t1 = await _logical_track(track_repo)
    bot = _bot(track_repo, sync_log)

    await bot._handle_reaction_count(_update(500, [_rc(DOWN, 2), _rc("❤", 5)]), MagicMock())

    bot._yt.remove_from_playlist.assert_called_once_with("vidZ")
    bot._sp.remove_from_playlist.assert_called_once_with("spZ")
    deleted = {c.kwargs["message_id"] for c in bot._app.bot.delete_message.await_args_list}
    assert deleted == {500, 501}
    assert await track_repo.get_track(t1.id) is None
    assert await track_repo.get_track_by_sp_track_id("spZ") is None

    bot._app.bot.send_message.assert_awaited_once()
    kw = bot._app.bot.send_message.await_args.kwargs
    assert kw["chat_id"] == CHANNEL
    assert "X — Song" in kw["text"]
    assert f"2 {DOWN}" in kw["text"]
    assert "YouTube Music" in kw["text"] and "Spotify" in kw["text"]
    assert "added again" in kw["text"]

    logs = await sync_log.get_recent_logs(5)
    assert any(entry.event == "track_removed_by_vote" for entry in logs)


async def test_reaction_on_status_card_counts(track_repo, sync_log) -> None:
    t1 = await _logical_track(track_repo)
    bot = _bot(track_repo, sync_log)
    await bot._handle_reaction_count(_update(501, [_rc(DOWN, 2)]), MagicMock())
    assert await track_repo.get_track(t1.id) is None


@pytest.mark.parametrize(
    "reactions",
    [
        [_rc(DOWN, 1)],
        [_rc("❤", 9)],  # other emojis never count
        [ReactionCount(type=ReactionTypeCustomEmoji("123"), total_count=5)],
        [],
    ],
)
async def test_below_threshold_is_noop(track_repo, sync_log, reactions) -> None:
    t1 = await _logical_track(track_repo)
    bot = _bot(track_repo, sync_log)
    await bot._handle_reaction_count(_update(500, reactions), MagicMock())
    assert await track_repo.get_track(t1.id) is not None
    bot._app.bot.send_message.assert_not_awaited()


async def test_disabled_or_other_chat_or_unknown_message_is_noop(track_repo, sync_log) -> None:
    t1 = await _logical_track(track_repo)
    off = _bot(track_repo, sync_log, threshold=0)
    await off._handle_reaction_count(_update(500, [_rc(DOWN, 5)]), MagicMock())
    other = _bot(track_repo, sync_log)
    await other._handle_reaction_count(_update(500, [_rc(DOWN, 5)], chat_id=-100999), MagicMock())
    # A non-track channel message (e.g. an announcement) is ignored.
    await other._handle_reaction_count(_update(9999, [_rc(DOWN, 5)]), MagicMock())
    assert await track_repo.get_track(t1.id) is not None
    off._app.bot.send_message.assert_not_awaited()
    other._app.bot.send_message.assert_not_awaited()


async def test_concurrent_audio_and_card_updates_remove_once(track_repo, sync_log) -> None:
    await _logical_track(track_repo)
    bot = _bot(track_repo, sync_log)
    await asyncio.gather(
        bot._handle_reaction_count(_update(500, [_rc(DOWN, 2)]), MagicMock()),
        bot._handle_reaction_count(_update(501, [_rc(DOWN, 3)]), MagicMock()),
    )
    bot._app.bot.send_message.assert_awaited_once()
    bot._yt.remove_from_playlist.assert_called_once()


async def test_track_can_be_added_again_after_vote_removal(track_repo, sync_log) -> None:
    # No blocklist: the same file posted again ingests as a fresh track.
    await _logical_track(track_repo)
    bot = _bot(track_repo, sync_log)
    await bot._handle_reaction_count(_update(500, [_rc(DOWN, 2)]), MagicMock())
    assert await track_repo.get_track_by_tg_file_unique_id("u1") is None
    again = await track_repo.create_track(
        direction="tg_to_yt", status="pending", title="Song", tg_file_id="f1",
        tg_file_unique_id="u1", tg_message_id=700,
    )
    assert again.id


async def test_announcement_failure_is_swallowed(track_repo, sync_log) -> None:
    t1 = await _logical_track(track_repo)
    bot = _bot(track_repo, sync_log)
    bot._app.bot.send_message = AsyncMock(side_effect=RuntimeError("boom"))
    await bot._handle_reaction_count(_update(500, [_rc(DOWN, 2)]), MagicMock())
    assert await track_repo.get_track(t1.id) is None


# ── Push sync racing a purge must not leave the song on the target ───


async def test_push_rolls_back_add_when_row_purged_midflight(track_repo, sync_log) -> None:
    track = await track_repo.create_track(
        direction="tg_to_yt", status="pending", title="Song", artist="X", tg_file_id="f1",
    )
    tg = MagicMock(download_file=AsyncMock(return_value="/tmp/x.mp3"), cleanup=MagicMock())
    yt = MagicMock()
    yt.get_playlist_tracks = MagicMock(return_value=[])
    yt.find_best_match = MagicMock(return_value={"videoId": "v1", "title": "Song"})
    yt.is_in_playlist = MagicMock(return_value=False)

    def add_then_purge(vid):
        # The vote/delete lands while the add is in flight.
        asyncio.run_coroutine_threadsafe(track_repo.delete_tracks([track.id]), loop).result()

    loop = asyncio.get_running_loop()
    yt.add_to_playlist = MagicMock(side_effect=add_then_purge)
    sync = TgToYtSync(track_repo, sync_log, tg, yt)
    with patch("navaar.sync._base_push.identify_track", return_value=None):
        await sync.process_pending()

    yt.remove_from_playlist.assert_called_once_with("v1")
    assert await track_repo.get_track(track.id) is None


async def test_raw_reaction_count_update_is_routed_to_handler(track_repo, sync_log) -> None:
    from telegram import Update
    from telegram.ext import MessageReactionHandler

    bot = _bot(track_repo, sync_log)
    app = bot.build_app()
    handlers = [
        h for group in app.handlers.values() for h in group
        if isinstance(h, MessageReactionHandler)
    ]

    def raw(chat_id):
        return Update.de_json({
            "update_id": 1,
            "message_reaction_count": {
                "chat": {"id": chat_id, "type": "channel", "title": "c"},
                "message_id": 500,
                "date": 1700000000,
                "reactions": [{"type": {"type": "emoji", "emoji": DOWN}, "total_count": 2}],
            },
        }, app.bot)

    assert len(handlers) == 1
    assert handlers[0].check_update(raw(CHANNEL))
    assert not handlers[0].check_update(raw(-100999))
