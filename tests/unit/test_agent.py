from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

from navaar.db.repository import SyncStateRepository, TrackRepository
from navaar.spotify.client import SpotifyClient
from navaar.telegram.agent import NavaarAgent
from navaar.ytmusic.client import YTMusicClient


def _agent(track_repo, tmp_path, *, yt=None, sp=None, engine=None, card=None, sp_enabled=True):
    return NavaarAgent(
        model="claude-sonnet-4-6",
        timeout=30,
        max_turns=8,
        workspace_dir=str(tmp_path / "agent"),
        bot=MagicMock(delete_message=AsyncMock()),
        channel_id=-1003744100092,
        track_repo=track_repo,
        sync_state=SyncStateRepository(track_repo._sf),
        engine=engine or MagicMock(),
        card_service=card or MagicMock(refresh=AsyncMock()),
        yt_client=yt or MagicMock(),
        sp_client=sp or MagicMock(),
        sp_enabled=sp_enabled,
    )


async def _mk_logical(repo: TrackRepository, *, yt="synced", sp="synced"):
    primary = await repo.create_track(
        direction="tg_to_yt", status=yt, title="Bohemian Rhapsody", artist="Queen",
        tg_file_id="FID", tg_file_unique_id="UID", tg_message_id=100,
        yt_video_id="YTVID", card_message_id=200,
    )
    await repo.create_track(
        direction="tg_to_sp", status=sp, title="Bohemian Rhapsody", artist="Queen",
        tg_file_id="FID", sp_track_id="SPID", card_message_id=200,
    )
    return await repo.get_sibling_tracks(primary)


def _result(text: str, *, session_id: str = "s", usage: dict | None = None) -> ResultMessage:
    return ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
        num_turns=1, session_id=session_id, result=text, usage=usage or {},
        total_cost_usd=0.01,
    )


def _assistant(text: str) -> AssistantMessage:
    return AssistantMessage(content=[TextBlock(text=text)], model="claude-sonnet-4-6")


def _stream(*messages):
    async def gen(*args, **kwargs):
        for m in messages:
            yield m
    return gen


# ── run(): result extraction over a mocked query() ───────────────────


async def test_run_returns_result_message(track_repo, tmp_path) -> None:
    agent = _agent(track_repo, tmp_path)
    with patch("navaar.telegram.agent.query", _stream(_assistant("thinking…"), _result("All done!"))):
        out = await agent.run(message_text="do something", siblings=None)
    assert out == "All done!"


async def test_run_falls_back_to_assistant_text(track_repo, tmp_path) -> None:
    # No ResultMessage with text → concatenate assistant text blocks.
    agent = _agent(track_repo, tmp_path)
    with patch("navaar.telegram.agent.query", _stream(_assistant("partial answer"))):
        out = await agent.run(message_text="hi", siblings=None)
    assert out == "partial answer"


async def test_run_swallows_errors(track_repo, tmp_path) -> None:
    agent = _agent(track_repo, tmp_path)

    def boom(*a, **k):
        raise RuntimeError("cli exploded")

    with patch("navaar.telegram.agent.query", boom):
        out = await agent.run(message_text="hi", siblings=None)
    assert "error" in out.lower()


async def test_run_disabled_is_noop(track_repo, tmp_path) -> None:
    agent = _agent(track_repo, tmp_path)
    agent.enabled = False
    with patch("navaar.telegram.agent.query", _stream(_result("x"))) as q:
        out = await agent.run(message_text="hi", siblings=None)
    assert out == ""
    q.assert_not_called() if hasattr(q, "assert_not_called") else None


async def test_run_includes_track_context_in_prompt(track_repo, tmp_path) -> None:
    agent = _agent(track_repo, tmp_path)
    siblings = await _mk_logical(track_repo)
    captured = {}

    async def gen(*args, **kwargs):
        captured["prompt"] = kwargs.get("prompt")
        yield _result("ok")

    with patch("navaar.telegram.agent.query", gen):
        await agent.run(message_text="unsync this", siblings=siblings)
    assert f"#{siblings[0].id}" in captured["prompt"]
    assert "Bohemian Rhapsody" in captured["prompt"]


# ── Session memory + management ──────────────────────────────────────


async def test_run_persists_and_resumes_session(track_repo, tmp_path) -> None:
    agent = _agent(track_repo, tmp_path)
    captured = []

    async def gen(prompt=None, options=None):
        captured.append(options.resume)
        yield _result("hi", session_id="SID-1")

    with patch("navaar.telegram.agent.query", gen):
        await agent.run(message_text="hello", siblings=None)
        await agent.run(message_text="again", siblings=None)
    # first run starts fresh (resume=None), second resumes the stored session
    assert captured == [None, "SID-1"]


async def test_reset_deletes_session_and_clears(track_repo, tmp_path) -> None:
    agent = _agent(track_repo, tmp_path)

    async def gen(prompt=None, options=None):
        yield _result("hi", session_id="SID-9")

    with patch("navaar.telegram.agent.query", gen):
        await agent.run(message_text="x", siblings=None)
    with patch("navaar.telegram.agent.delete_session") as ds:
        out = await agent.reset()
    ds.assert_called_once_with("SID-9", agent._workspace)
    assert agent._session_id is None
    assert "reset" in out.lower()
    # next message starts a fresh session
    captured = []

    async def gen2(prompt=None, options=None):
        captured.append(options.resume)
        yield _result("hi", session_id="SID-NEW")

    with patch("navaar.telegram.agent.query", gen2):
        await agent.run(message_text="y", siblings=None)
    assert captured == [None]


async def test_context_info(track_repo, tmp_path) -> None:
    agent = _agent(track_repo, tmp_path)
    assert "No active conversation" in await agent.context_info()

    async def gen(prompt=None, options=None):
        yield _result("hi", session_id="SID", usage={
            "input_tokens": 1000, "output_tokens": 200, "cache_read_input_tokens": 5000,
        })

    with patch("navaar.telegram.agent.query", gen):
        await agent.run(message_text="x", siblings=None)
    with patch("navaar.telegram.agent.get_session_info", return_value=None):
        out = await agent.context_info()
    assert "Conversation context" in out
    assert "Messages: 1" in out


async def test_compact_summarizes_and_reseeds(track_repo, tmp_path) -> None:
    agent = _agent(track_repo, tmp_path)

    async def seed(prompt=None, options=None):
        yield _result("hi", session_id="OLD")

    with patch("navaar.telegram.agent.query", seed):
        await agent.run(message_text="x", siblings=None)

    order = []

    async def gen(prompt=None, options=None):
        order.append(options.resume)
        sid = "OLD" if options.resume == "OLD" else "NEW"
        yield _result("summary" if options.resume == "OLD" else "ack", session_id=sid)

    with patch("navaar.telegram.agent.query", gen), \
            patch("navaar.telegram.agent.delete_session") as ds:
        out = await agent.compact()
    ds.assert_called_once_with("OLD", agent._workspace)
    assert agent._session_id == "NEW"
    assert order == ["OLD", None]  # summarize the old, reseed a fresh one
    assert "Compacted" in out


# ── Core mutation logic (what the MCP tools call) ────────────────────


async def test_do_unsync_removes_from_both(track_repo, tmp_path) -> None:
    yt = MagicMock(remove_from_playlist=MagicMock(return_value=True))
    sp = MagicMock(remove_from_playlist=MagicMock(return_value=None))
    card = MagicMock(refresh=AsyncMock())
    agent = _agent(track_repo, tmp_path, yt=yt, sp=sp, card=card)
    siblings = await _mk_logical(track_repo)

    out = await agent._do_unsync(siblings, "all")

    yt.remove_from_playlist.assert_called_once_with("YTVID")
    sp.remove_from_playlist.assert_called_once_with("SPID")
    card.refresh.assert_awaited_once()
    assert "YouTube Music" in out and "Spotify" in out
    for s in await track_repo.get_sibling_tracks(siblings[0]):
        assert (await track_repo.get_track(s.id)).status == "unsynced"


async def test_do_unsync_single_platform(track_repo, tmp_path) -> None:
    yt = MagicMock(remove_from_playlist=MagicMock(return_value=True))
    sp = MagicMock(remove_from_playlist=MagicMock(return_value=None))
    agent = _agent(track_repo, tmp_path, yt=yt, sp=sp)
    siblings = await _mk_logical(track_repo)
    await agent._do_unsync(siblings, "sp")
    sp.remove_from_playlist.assert_called_once_with("SPID")
    yt.remove_from_playlist.assert_not_called()


async def test_do_resync_forces_sync(track_repo, tmp_path) -> None:
    engine = MagicMock(force_sync=MagicMock())
    agent = _agent(track_repo, tmp_path, engine=engine)
    siblings = await _mk_logical(track_repo, yt="unsynced", sp="failed")
    await agent._do_resync(siblings, "all")
    forced = {c.args[0] for c in engine.force_sync.call_args_list}
    assert forced == {"tg_to_yt", "tg_to_sp"}
    for s in await track_repo.get_sibling_tracks(siblings[0]):
        assert (await track_repo.get_track(s.id)).status == "retry_scheduled"


async def test_do_delete_removes_rows_and_messages(track_repo, tmp_path) -> None:
    agent = _agent(track_repo, tmp_path, yt=MagicMock(remove_from_playlist=MagicMock(return_value=True)))
    siblings = await _mk_logical(track_repo)
    ids = [s.id for s in siblings]
    out = await agent._do_delete(siblings)
    for tid in ids:
        assert await track_repo.get_track(tid) is None
    deleted = {c.kwargs["message_id"] for c in agent._bot.delete_message.call_args_list}
    assert deleted == {100, 200}  # audio message + status card
    assert "Deleted" in out


async def test_delete_message_tool(track_repo, tmp_path) -> None:
    agent = _agent(track_repo, tmp_path)
    out = await agent._delete_message(555)
    agent._bot.delete_message.assert_awaited_once_with(chat_id=-1003744100092, message_id=555)
    assert "Deleted channel message 555" in out


async def test_status_text_renders(track_repo, tmp_path) -> None:
    agent = _agent(track_repo, tmp_path)
    siblings = await _mk_logical(track_repo)
    out = agent._status_text(siblings)
    assert "Queen — Bohemian Rhapsody" in out
    assert "music.youtube.com/watch?v=YTVID" in out
    assert "open.spotify.com/track/SPID" in out


async def test_resolve_unknown_track(track_repo, tmp_path) -> None:
    agent = _agent(track_repo, tmp_path)
    assert await agent._resolve(999) is None
    assert await agent._resolve(None) is None


# ── Client removal methods ───────────────────────────────────────────


def test_yt_remove_from_playlist_deletes_by_set_video_id() -> None:
    inst = object.__new__(YTMusicClient)
    inst._playlist_id = "PL"
    inst._headers = lambda: {"Authorization": "Bearer x"}
    resp = MagicMock(raise_for_status=MagicMock())
    with patch("navaar.ytmusic.client.httpx.delete", return_value=resp) as deleter:
        ok = inst.remove_from_playlist(
            "VID", playlist_tracks=[{"videoId": "VID", "setVideoId": "SET123"}]
        )
    assert ok is True
    assert deleter.call_args.kwargs["params"] == {"id": "SET123"}


def test_sp_remove_from_playlist_calls_spotipy() -> None:
    inst = object.__new__(SpotifyClient)
    inst._playlist_id = "PL"
    inst._sp = MagicMock()
    inst.remove_from_playlist("TID")
    inst._sp.playlist_remove_all_occurrences_of_items.assert_called_once_with("PL", ["TID"])
