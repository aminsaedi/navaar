from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

import navaar.telegram.voice_id as vi
from navaar.telegram.voice_id import VoiceIdentifier


def _voice_message(*, file_size: int = 1024, chat_id: int = -100):
    msg = MagicMock()
    msg.chat_id = chat_id
    msg.message_id = 42
    msg.voice = MagicMock(file_id="vfid", file_size=file_size)
    msg.reply_text = AsyncMock()
    return msg


def _update(message):
    upd = MagicMock()
    upd.channel_post = message
    return upd


def _context():
    ctx = MagicMock()
    ctx.bot.get_file = AsyncMock(
        return_value=MagicMock(download_to_drive=AsyncMock())
    )
    return ctx


def _identifier(*, in_playlist=False, existing=None, match=True):
    yt = MagicMock()
    yt.find_best_match.return_value = {"videoId": "vid123"} if match else None
    yt.get_playlist_tracks.return_value = []
    yt.is_in_playlist.return_value = in_playlist
    tracks = MagicMock(get_track_by_yt_video_id=AsyncMock(return_value=existing))
    log = MagicMock(log=AsyncMock())
    return VoiceIdentifier(MagicMock(), yt, tracks, log, channel_id=-100), yt, tracks


@pytest.mark.asyncio
async def test_voice_message_is_identified_and_added(monkeypatch) -> None:
    async def fake_recognize(_path):
        return ("Faramarz Aslani", "Yar")

    monkeypatch.setattr("navaar.telegram.voice_id.recognize", fake_recognize)
    ident, yt, _ = _identifier()
    msg = _voice_message()
    await ident.handle(_update(msg), _context())
    yt.add_to_playlist.assert_called_once_with("vid123")
    msg.reply_text.assert_awaited()
    assert "Added" in msg.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_voice_message_already_in_playlist_is_not_readded(monkeypatch) -> None:
    async def fake_recognize(_path):
        return ("Artist", "Title")

    monkeypatch.setattr("navaar.telegram.voice_id.recognize", fake_recognize)
    ident, yt, _ = _identifier(in_playlist=True)
    msg = _voice_message()
    await ident.handle(_update(msg), _context())
    yt.add_to_playlist.assert_not_called()
    assert "Skipped" in msg.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_voice_message_too_big_is_ignored() -> None:
    ident, yt, _ = _identifier()
    msg = _voice_message(file_size=25 * 1024 * 1024)
    await ident.handle(_update(msg), _context())
    yt.find_best_match.assert_not_called()
    msg.reply_text.assert_not_awaited()


def _fake_shazamio(monkeypatch, fake_recognize):
    """Install a fake ``shazamio`` module whose Shazam calls ``fake_recognize``."""

    class FakeShazam:
        async def recognize(self, path):
            return fake_recognize(path)

        async def close(self):
            pass

    fake_mod = MagicMock(Shazam=FakeShazam)
    monkeypatch.setitem(sys.modules, "shazamio", fake_mod)
    return fake_mod


def _fake_transcode(monkeypatch, *, duration: float = 9.0):
    calls: list[tuple[str, int]] = []

    async def fake_to_wav(src, dest, *, loops: int = 1):
        calls.append((str(dest), loops))
        return True

    async def fake_duration(_path):
        return duration

    monkeypatch.setattr(vi, "_to_wav", fake_to_wav)
    monkeypatch.setattr(vi, "_duration", fake_duration)
    return calls


@pytest.mark.asyncio
async def test_recognize_returns_none_without_title(monkeypatch) -> None:
    _fake_transcode(monkeypatch)
    _fake_shazamio(monkeypatch, lambda _path: {"track": {"title": ""}})
    assert await vi.recognize("/tmp/x.ogg") is None


@pytest.mark.asyncio
async def test_recognize_transcodes_then_identifies(monkeypatch) -> None:
    calls = _fake_transcode(monkeypatch)
    _fake_shazamio(monkeypatch, lambda _path: {"track": {"title": "Hello", "subtitle": "Adele"}})

    assert await vi.recognize("/tmp/x.ogg") == ("Adele", "Hello")
    # Direct hit: only the canonical WAV is produced, no looped retry.
    assert [loops for _, loops in calls] == [1]


@pytest.mark.asyncio
async def test_recognize_retries_with_looped_clip(monkeypatch) -> None:
    calls = _fake_transcode(monkeypatch, duration=9.0)

    def fake_recognize(path):
        if path.endswith("voice_loop.wav"):
            return {"track": {"title": "Nazanin", "subtitle": "Dariush"}}
        return {"matches": []}

    _fake_shazamio(monkeypatch, fake_recognize)

    assert await vi.recognize("/tmp/x.ogg") == ("Dariush", "Nazanin")
    assert [loops for _, loops in calls] == [1, 4]  # 9 s clip -> ~36 s


@pytest.mark.asyncio
async def test_voice_failure_still_replies(monkeypatch) -> None:
    async def boom(_path):
        raise RuntimeError("shazam exploded")

    monkeypatch.setattr("navaar.telegram.voice_id.recognize", boom)
    ident, yt, _ = _identifier()
    msg = _voice_message()

    await ident.handle(_update(msg), _context())

    yt.add_to_playlist.assert_not_called()
    msg.reply_text.assert_awaited()
    assert "Couldn't identify" in msg.reply_text.await_args.args[0]
