from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from navaar.db.repository import TrackRepository
from navaar.spotify.client import SpotifyClient
from navaar.telegram.agent import NavaarAgent
from navaar.ytmusic.client import YTMusicClient


def _httpx_returning(content: str | None = None, raise_exc: Exception | None = None):
    """Build a replacement for httpx.AsyncClient that yields a client whose .post
    returns a chat-completions response with the given content (or raises)."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={"choices": [{"message": {"content": content}}]})
    client = MagicMock()
    client.post = (
        AsyncMock(side_effect=raise_exc) if raise_exc else AsyncMock(return_value=resp)
    )
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=cm)


def _agent(track_repo, *, yt=None, sp=None, engine=None, card=None, sp_enabled=True):
    return NavaarAgent(
        base_url="http://nl.test/v1",
        api_key="",
        model="claude-haiku-4-5",
        timeout=5,
        bot=MagicMock(delete_message=AsyncMock()),
        channel_id=-1003744100092,
        track_repo=track_repo,
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


# ── Intent parsing ───────────────────────────────────────────────────


async def test_parse_intent_strips_fences(track_repo: TrackRepository) -> None:
    agent = _agent(track_repo)
    content = '```json\n{"action":"unsync","platform":"sp","track_id":null,"reply":"ok"}\n```'
    with patch("navaar.telegram.agent.httpx.AsyncClient", _httpx_returning(content)):
        intent = await agent.parse_intent("ctx", "remove from spotify")
    assert intent == {"action": "unsync", "platform": "sp", "track_id": None, "reply": "ok"}


async def test_parse_intent_bare_json(track_repo: TrackRepository) -> None:
    agent = _agent(track_repo)
    content = '{"action":"status","platform":null,"track_id":42,"reply":"here"}'
    with patch("navaar.telegram.agent.httpx.AsyncClient", _httpx_returning(content)):
        intent = await agent.parse_intent("ctx", "status of #42")
    assert intent["action"] == "status"
    assert intent["track_id"] == 42


async def test_parse_intent_prose_wrapped_json(track_repo: TrackRepository) -> None:
    agent = _agent(track_repo)
    content = 'Sure! Here you go:\n{"action":"delete","platform":"all","track_id":null,"reply":"gone"}\nDone.'
    with patch("navaar.telegram.agent.httpx.AsyncClient", _httpx_returning(content)):
        intent = await agent.parse_intent("ctx", "delete it")
    assert intent["action"] == "delete"
    assert intent["platform"] == "all"


async def test_parse_intent_invalid_action_falls_back(track_repo: TrackRepository) -> None:
    agent = _agent(track_repo)
    content = '{"action":"explode","platform":"zz","track_id":"x","reply":"hmm"}'
    with patch("navaar.telegram.agent.httpx.AsyncClient", _httpx_returning(content)):
        intent = await agent.parse_intent("ctx", "do something weird")
    assert intent["action"] == "none"
    assert intent["platform"] is None
    assert intent["track_id"] is None


async def test_parse_intent_network_error_is_none(track_repo: TrackRepository) -> None:
    agent = _agent(track_repo)
    with patch(
        "navaar.telegram.agent.httpx.AsyncClient",
        _httpx_returning(raise_exc=RuntimeError("boom")),
    ):
        intent = await agent.parse_intent("ctx", "anything")
    assert intent["action"] == "none"


# ── Action execution ─────────────────────────────────────────────────


async def test_unsync_removes_from_both_and_marks_unsynced(track_repo: TrackRepository) -> None:
    yt = MagicMock(remove_from_playlist=MagicMock(return_value=True))
    sp = MagicMock(remove_from_playlist=MagicMock(return_value=None))
    card = MagicMock(refresh=AsyncMock())
    agent = _agent(track_repo, yt=yt, sp=sp, card=card)
    siblings = await _mk_logical(track_repo)

    with patch.object(agent, "parse_intent", AsyncMock(
        return_value={"action": "unsync", "platform": "all", "track_id": None, "reply": ""}
    )):
        result = await agent.run(message_text="unsync this", siblings=siblings)

    yt.remove_from_playlist.assert_called_once_with("YTVID")
    sp.remove_from_playlist.assert_called_once_with("SPID")
    card.refresh.assert_awaited_once()
    assert "Spotify" in result and "YouTube Music" in result
    # both target rows now unsynced
    for s in await track_repo.get_sibling_tracks(siblings[0]):
        if s.direction in ("tg_to_yt", "tg_to_sp"):
            assert (await track_repo.get_track(s.id)).status == "unsynced"


async def test_unsync_single_platform(track_repo: TrackRepository) -> None:
    yt = MagicMock(remove_from_playlist=MagicMock(return_value=True))
    sp = MagicMock(remove_from_playlist=MagicMock(return_value=None))
    agent = _agent(track_repo, yt=yt, sp=sp)
    siblings = await _mk_logical(track_repo)

    with patch.object(agent, "parse_intent", AsyncMock(
        return_value={"action": "unsync", "platform": "sp", "track_id": None, "reply": ""}
    )):
        await agent.run(message_text="off spotify", siblings=siblings)

    sp.remove_from_playlist.assert_called_once_with("SPID")
    yt.remove_from_playlist.assert_not_called()


async def test_resync_resets_and_forces_sync(track_repo: TrackRepository) -> None:
    engine = MagicMock(force_sync=MagicMock())
    agent = _agent(track_repo, engine=engine)
    siblings = await _mk_logical(track_repo, yt="unsynced", sp="failed")

    with patch.object(agent, "parse_intent", AsyncMock(
        return_value={"action": "resync", "platform": "all", "track_id": None, "reply": ""}
    )):
        await agent.run(message_text="resync everywhere", siblings=siblings)

    forced = {c.args[0] for c in engine.force_sync.call_args_list}
    assert forced == {"tg_to_yt", "tg_to_sp"}
    for s in await track_repo.get_sibling_tracks(siblings[0]):
        assert (await track_repo.get_track(s.id)).status == "retry_scheduled"


async def test_delete_removes_rows_and_card(track_repo: TrackRepository) -> None:
    bot = MagicMock(delete_message=AsyncMock())
    yt = MagicMock(remove_from_playlist=MagicMock(return_value=True))
    sp = MagicMock(remove_from_playlist=MagicMock(return_value=None))
    agent = _agent(track_repo, yt=yt, sp=sp)
    agent._bot = bot
    siblings = await _mk_logical(track_repo)
    ids = [s.id for s in siblings]

    with patch.object(agent, "parse_intent", AsyncMock(
        return_value={"action": "delete", "platform": None, "track_id": None, "reply": ""}
    )):
        result = await agent.run(message_text="delete it", siblings=siblings)

    bot.delete_message.assert_awaited_once()
    for tid in ids:
        assert await track_repo.get_track(tid) is None
    assert "Deleted" in result


async def test_status_renders_without_mutation(track_repo: TrackRepository) -> None:
    agent = _agent(track_repo)
    siblings = await _mk_logical(track_repo)

    with patch.object(agent, "parse_intent", AsyncMock(
        return_value={"action": "status", "platform": None, "track_id": None, "reply": ""}
    )):
        result = await agent.run(message_text="where is it?", siblings=siblings)

    assert "Queen — Bohemian Rhapsody" in result
    assert "music.youtube.com/watch?v=YTVID" in result
    assert "open.spotify.com/track/SPID" in result
    # unchanged
    assert (await track_repo.get_track(siblings[0].id)).status == "synced"


async def test_none_returns_model_reply(track_repo: TrackRepository) -> None:
    agent = _agent(track_repo)
    with patch.object(agent, "parse_intent", AsyncMock(
        return_value={"action": "none", "platform": None, "track_id": None, "reply": "Hello!"}
    )):
        result = await agent.run(message_text="hi", siblings=None)
    assert result == "Hello!"


async def test_dm_resolves_track_by_id(track_repo: TrackRepository) -> None:
    yt = MagicMock(remove_from_playlist=MagicMock(return_value=True))
    agent = _agent(track_repo, yt=yt)
    siblings = await _mk_logical(track_repo)
    primary_id = siblings[0].id

    with patch.object(agent, "parse_intent", AsyncMock(
        return_value={"action": "unsync", "platform": "yt", "track_id": primary_id, "reply": ""}
    )):
        await agent.run(message_text=f"unsync #{primary_id} from youtube", siblings=None)

    yt.remove_from_playlist.assert_called_once_with("YTVID")


# ── Client removal methods ───────────────────────────────────────────


def test_yt_remove_from_playlist_deletes_by_set_video_id() -> None:
    inst = object.__new__(YTMusicClient)
    inst._playlist_id = "PL"
    inst._headers = lambda: {"Authorization": "Bearer x"}
    playlist = [{"videoId": "VID", "setVideoId": "SET123"}]
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    with patch("navaar.ytmusic.client.httpx.delete", return_value=resp) as deleter:
        ok = inst.remove_from_playlist("VID", playlist_tracks=playlist)
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
