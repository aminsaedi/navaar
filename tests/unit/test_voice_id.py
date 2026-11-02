from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

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


@pytest.mark.asyncio
async def test_recognize_returns_none_without_title(monkeypatch) -> None:
    # recognize imports shazamio lazily; stub the client to avoid network.
    import navaar.telegram.voice_id as vi

    class FakeShazam:
        async def recognize(self, _path):
            return {"track": {"title": ""}}

    fake_mod = MagicMock(Shazam=FakeShazam)
    monkeypatch.setitem(__import__("sys").modules, "shazamio", fake_mod)
    assert await vi.recognize("/tmp/x.ogg") is None
