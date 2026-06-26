from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

from navaar.db.repository import TrackRepository
from navaar.spotify.client import SpotifyClient
from navaar.telegram.agent import NavaarAgent
from navaar.ytmusic.client import YTMusicClient


def _agent(track_repo, *, yt=None, sp=None, engine=None, card=None,
           sp_enabled=True, shell=False):
    return NavaarAgent(
        base_url="http://nl.test/v1", api_key="", model="claude-haiku-4-5", timeout=5,
        bot=MagicMock(delete_message=AsyncMock()), channel_id=-1003744100092,
        track_repo=track_repo, engine=engine or MagicMock(),
        card_service=card or MagicMock(refresh=AsyncMock()),
        yt_client=yt or MagicMock(), sp_client=sp or MagicMock(),
        sp_enabled=sp_enabled, shell_enabled=shell, max_iterations=6,
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


def _tool(name, **args):
    return json.dumps({"tool": name, "args": args})


def _final(msg):
    return json.dumps({"final": msg})


# ── first-JSON parsing ───────────────────────────────────────────────


def test_first_json_ignores_trailing_hallucination() -> None:
    raw = '{"tool":"sql","args":{"query":"SELECT 1"}}\n\n{"tool_result":[1]}\n{"final":"fake"}'
    obj = NavaarAgent._first_json(raw)
    assert obj == {"tool": "sql", "args": {"query": "SELECT 1"}}


def test_first_json_handles_fences_and_braces_in_strings() -> None:
    assert NavaarAgent._first_json('```json\n{"final":"a } b"}\n```') == {"final": "a } b"}
    assert NavaarAgent._first_json("no json here") is None


# ── Loop ─────────────────────────────────────────────────────────────


async def test_loop_runs_tool_then_returns_final(track_repo: TrackRepository) -> None:
    siblings = await _mk_logical(track_repo)
    agent = _agent(track_repo)
    with patch.object(agent, "_chat", AsyncMock(side_effect=[
        _tool("find_duplicates"),
        _final("all done"),
    ])):
        result = await agent.run(message_text="any dupes?", siblings=siblings)
    assert result == "all done"


async def test_loop_executes_real_tool_not_hallucination(track_repo: TrackRepository) -> None:
    # The model emits a tool call AND a fabricated result+final in one reply; the
    # loop must run the real tool and keep going, not return the fake final.
    yt = MagicMock(remove_from_playlist=MagicMock(return_value=True))
    sp = MagicMock(remove_from_playlist=MagicMock(return_value=None))
    siblings = await _mk_logical(track_repo)
    agent = _agent(track_repo, yt=yt, sp=sp)
    with patch.object(agent, "_chat", AsyncMock(side_effect=[
        _tool("unsync", platform="all") + '\n{"final":"already done"}',
        _final("removed it"),
    ])):
        result = await agent.run(message_text="unsync this", siblings=siblings)
    assert result == "removed it"
    yt.remove_from_playlist.assert_called_once_with("YTVID")  # real tool ran
    sp.remove_from_playlist.assert_called_once_with("SPID")


async def test_loop_iteration_cap_returns_fallback(track_repo: TrackRepository) -> None:
    agent = _agent(track_repo)
    agent._max_iter = 2
    with patch.object(agent, "_chat", AsyncMock(return_value=_tool("find_duplicates"))):
        result = await agent.run(message_text="loop forever", siblings=None)
    assert "too many steps" in result


async def test_loop_unknown_tool_recovers(track_repo: TrackRepository) -> None:
    agent = _agent(track_repo)
    with patch.object(agent, "_chat", AsyncMock(side_effect=[
        _tool("nonexistent"),
        _final("recovered"),
    ])):
        result = await agent.run(message_text="do x", siblings=None)
    assert result == "recovered"


async def test_loop_request_failure_returns_message(track_repo: TrackRepository) -> None:
    agent = _agent(track_repo)
    with patch.object(agent, "_chat", AsyncMock(side_effect=RuntimeError("down"))):
        result = await agent.run(message_text="hi", siblings=None)
    assert "couldn't reach" in result


async def test_disabled_agent_noop(track_repo: TrackRepository) -> None:
    agent = _agent(track_repo)
    agent.enabled = False
    assert await agent.run(message_text="x", siblings=None) == ""


# ── Tool registry gating ─────────────────────────────────────────────


def test_shell_tool_absent_unless_enabled(track_repo: TrackRepository) -> None:
    assert "shell" not in _agent(track_repo, shell=False)._tools
    assert "shell" in _agent(track_repo, shell=True)._tools


# ── Individual tools ─────────────────────────────────────────────────


async def test_tool_status_uses_context_track(track_repo: TrackRepository) -> None:
    siblings = await _mk_logical(track_repo)
    agent = _agent(track_repo)
    out = await agent._tool_status({}, siblings[0].id)
    assert "Queen — Bohemian Rhapsody" in out
    assert "music.youtube.com/watch?v=YTVID" in out


async def test_tool_unsync_resolves_by_track_id(track_repo: TrackRepository) -> None:
    yt = MagicMock(remove_from_playlist=MagicMock(return_value=True))
    agent = _agent(track_repo, yt=yt)
    siblings = await _mk_logical(track_repo)
    out = await agent._tool_unsync({"track_id": siblings[0].id, "platform": "yt"}, None)
    yt.remove_from_playlist.assert_called_once_with("YTVID")
    assert "YouTube Music" in out


async def test_tool_resync_forces_sync(track_repo: TrackRepository) -> None:
    engine = MagicMock(force_sync=MagicMock())
    agent = _agent(track_repo, engine=engine)
    siblings = await _mk_logical(track_repo, yt="unsynced", sp="failed")
    await agent._tool_resync({"platform": "all"}, siblings[0].id)
    forced = {c.args[0] for c in engine.force_sync.call_args_list}
    assert forced == {"tg_to_yt", "tg_to_sp"}


async def test_tool_delete_removes_rows(track_repo: TrackRepository) -> None:
    agent = _agent(track_repo, yt=MagicMock(remove_from_playlist=MagicMock(return_value=True)))
    siblings = await _mk_logical(track_repo)
    ids = [s.id for s in siblings]
    out = await agent._tool_delete({}, siblings[0].id)
    for tid in ids:
        assert await track_repo.get_track(tid) is None
    assert "Deleted" in out


async def test_tool_sql_returns_rows(track_repo: TrackRepository) -> None:
    await _mk_logical(track_repo)
    agent = _agent(track_repo)
    out = await agent._tool_sql({"query": "SELECT COUNT(*) AS n FROM tracks"}, None)
    assert json.loads(out) == [{"n": 2}]


async def test_tool_sql_rejects_mutations(track_repo: TrackRepository) -> None:
    agent = _agent(track_repo)
    out = await agent._tool_sql({"query": "DELETE FROM tracks"}, None)
    assert "SQL error" in out


async def test_tool_shell_runs_command(track_repo: TrackRepository) -> None:
    agent = _agent(track_repo, shell=True)

    async def fake_exec(*a, **k):
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(b"hello world\n", None))
        proc.kill = MagicMock()
        return proc

    with patch("navaar.telegram.agent.asyncio.create_subprocess_exec", side_effect=fake_exec):
        out = await agent._tool_shell({"command": "echo hello world"}, None)
    assert "hello world" in out


async def test_tool_shell_times_out(track_repo: TrackRepository) -> None:
    agent = _agent(track_repo, shell=True)
    agent._shell_timeout = 0.05

    async def fake_exec(*a, **k):
        proc = AsyncMock()

        async def slow():
            await asyncio.sleep(0.5)
            return (b"x", None)

        proc.communicate = slow
        proc.kill = MagicMock()
        return proc

    with patch("navaar.telegram.agent.asyncio.create_subprocess_exec", side_effect=fake_exec):
        out = await agent._tool_shell({"command": "sleep 10"}, None)
    assert "timed out" in out


# ── Repository run_select ────────────────────────────────────────────


async def test_run_select_returns_dicts(track_repo: TrackRepository) -> None:
    await _mk_logical(track_repo)
    rows = await track_repo.run_select("SELECT direction, status FROM tracks ORDER BY id")
    assert {r["direction"] for r in rows} == {"tg_to_yt", "tg_to_sp"}


async def test_run_select_rejects_non_select(track_repo: TrackRepository) -> None:
    for bad in ["DELETE FROM tracks", "UPDATE tracks SET status='x'", "SELECT 1; DROP TABLE tracks"]:
        try:
            await track_repo.run_select(bad)
            raise AssertionError(f"should have rejected: {bad}")
        except ValueError:
            pass


# ── Find duplicates ──────────────────────────────────────────────────


async def _mk_channel_track(repo, *, title, artist, msg, fid):
    return await repo.create_track(
        direction="tg_to_yt", status="synced", title=title, artist=artist,
        tg_message_id=msg, tg_file_id=fid, tg_file_unique_id=fid,
    )


async def test_get_channel_tracks_excludes_non_anchor_rows(track_repo: TrackRepository) -> None:
    await _mk_channel_track(track_repo, title="A", artist="X", msg=10, fid="f1")
    await track_repo.create_track(
        direction="tg_to_sp", status="synced", title="A", artist="X", tg_file_id="f1"
    )
    anchors = await track_repo.get_channel_tracks()
    assert [t.tg_message_id for t in anchors] == [10]


async def test_find_duplicates_groups_normalized(track_repo: TrackRepository) -> None:
    await _mk_channel_track(track_repo, title="Song One", artist="X", msg=10, fid="f1")
    await _mk_channel_track(track_repo, title="song one", artist=" x ", msg=11, fid="f2")
    await _mk_channel_track(track_repo, title="Other", artist="Y", msg=12, fid="f3")
    out = await _agent(track_repo)._find_duplicates()
    assert "1 duplicated song(s)" in out and "×2" in out
    assert "msg 10" in out and "msg 11" in out and "Other" not in out


async def test_find_duplicates_none(track_repo: TrackRepository) -> None:
    await _mk_channel_track(track_repo, title="Solo", artist="Z", msg=20, fid="g1")
    assert "No duplicate songs found" in await _agent(track_repo)._find_duplicates()


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


def test_yt_remove_from_playlist_absent_returns_false() -> None:
    inst = object.__new__(YTMusicClient)
    inst._playlist_id = "PL"
    inst._headers = lambda: {}
    with patch("navaar.ytmusic.client.httpx.delete") as deleter:
        ok = inst.remove_from_playlist("VID", playlist_tracks=[{"videoId": "OTHER"}])
    assert ok is False
    deleter.assert_not_called()


def test_sp_remove_from_playlist_calls_spotipy() -> None:
    inst = object.__new__(SpotifyClient)
    inst._playlist_id = "PL"
    inst._sp = MagicMock()
    inst.remove_from_playlist("TID")
    inst._sp.playlist_remove_all_occurrences_of_items.assert_called_once_with("PL", ["TID"])
