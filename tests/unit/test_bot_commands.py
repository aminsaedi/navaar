from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from navaar.telegram.bot import NavaarBot


def _make_bot(sp_client: object | None = None) -> NavaarBot:
    bot = NavaarBot(
        token="x",
        channel_id=-100,
        admin_user_ids=[111, 222],
        track_repo=MagicMock(),
        sync_log=MagicMock(),
        sp_client=sp_client,
    )
    bot._app = MagicMock()
    bot._app.bot = MagicMock()
    bot._app.bot.set_my_commands = AsyncMock()
    return bot


def test_menu_excludes_search_sp_when_spotify_disabled() -> None:
    names = [c.command for c in _make_bot(sp_client=None)._menu_commands()]
    assert "search_sp" not in names
    # Core commands are always present.
    assert {"status", "sync", "retry", "help"} <= set(names)


def test_menu_includes_search_sp_when_spotify_enabled() -> None:
    names = [c.command for c in _make_bot(sp_client=MagicMock())._menu_commands()]
    assert "search_sp" in names


def test_menu_commands_match_registered_handlers() -> None:
    # Every menu entry must map to a real handler (no dangling menu items that
    # do nothing when tapped). Aliases (start/list_failed/force_sync) are allowed
    # to exist as handlers without appearing in the menu.
    bot = _make_bot(sp_client=MagicMock())
    app = bot.build_app()
    registered = {
        h.commands and next(iter(h.commands))
        for group in app.handlers.values()
        for h in group
        if hasattr(h, "commands") and h.commands
    }
    for cmd in bot._menu_commands():
        assert cmd.command in registered, f"menu command /{cmd.command} has no handler"


@pytest.mark.asyncio
async def test_set_command_menu_registers_per_admin_scope() -> None:
    bot = _make_bot()
    await bot.set_command_menu()
    # One setMyCommands call per admin, each scoped to that admin's own chat.
    assert bot._app.bot.set_my_commands.await_count == 2
    scopes = {
        call.kwargs["scope"].chat_id
        for call in bot._app.bot.set_my_commands.call_args_list
    }
    assert scopes == {111, 222}


@pytest.mark.asyncio
async def test_set_command_menu_swallows_errors() -> None:
    bot = _make_bot()
    bot._app.bot.set_my_commands = AsyncMock(side_effect=RuntimeError("chat not found"))
    # A failing admin chat must not propagate — startup continues regardless.
    await bot.set_command_menu()


@pytest.mark.asyncio
async def test_set_command_menu_noop_without_app() -> None:
    bot = _make_bot()
    bot._app = None
    await bot.set_command_menu()  # must not raise


def _channel_msg(text: str, *, reply_to: int | None = None) -> MagicMock:
    msg = MagicMock()
    msg.text = text
    msg.chat_id = -100
    msg.reply_to_message = MagicMock(message_id=reply_to) if reply_to else None
    msg.reply_text = AsyncMock()
    return msg


@pytest.mark.asyncio
async def test_channel_mention_without_reply_runs_agent() -> None:
    # A standalone @mention (no reply) is a channel-wide request, e.g. duplicates.
    bot = _make_bot(sp_client=MagicMock())
    bot._bot_username = "navbot"
    bot._agent = MagicMock(enabled=True, run=AsyncMock(return_value="dupes here"))
    msg = _channel_msg("@navbot list duplicate tracks")
    await bot._handle_channel_command(MagicMock(channel_post=msg), MagicMock())
    bot._agent.run.assert_awaited_once()
    assert bot._agent.run.await_args.kwargs["siblings"] is None
    msg.reply_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_channel_mention_reply_passes_track_context() -> None:
    bot = _make_bot(sp_client=MagicMock())
    bot._bot_username = "navbot"
    bot._agent = MagicMock(enabled=True, run=AsyncMock(return_value="ok"))
    bot._tracks = MagicMock(
        get_logical_track_by_message_id=AsyncMock(return_value=MagicMock()),
        get_sibling_tracks=AsyncMock(return_value=["sib"]),
    )
    msg = _channel_msg("@navbot unsync this", reply_to=42)
    await bot._handle_channel_command(MagicMock(channel_post=msg), MagicMock())
    assert bot._agent.run.await_args.kwargs["siblings"] == ["sib"]


@pytest.mark.asyncio
async def test_channel_text_without_mention_ignored() -> None:
    bot = _make_bot(sp_client=MagicMock())
    bot._bot_username = "navbot"
    bot._agent = MagicMock(enabled=True, run=AsyncMock())
    msg = _channel_msg("just chatting, no bot here")
    await bot._handle_channel_command(MagicMock(channel_post=msg), MagicMock())
    bot._agent.run.assert_not_called()
