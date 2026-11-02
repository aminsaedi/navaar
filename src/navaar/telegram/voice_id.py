"""Identify voice messages posted in the channel (Shazam) and add the best hit.

Anyone who can post in the channel can record a voice message of a song they
heard. For every channel voice message we:

1. download it (Bot API getFile — read-only, 20 MB cap),
2. ask Shazam (``shazamio``, unofficial but keyless) what it is,
3. resolve the Shazam artist/title to the best YouTube Music match,
4. add that video to the YouTube playlist **only** — never insert a ``tracks``
   row. The yt→tg / yt→sp loops see the new playlist entry via snapshot diffing
   and create the authoritative rows, download the audio and fan out to Spotify.

Safety rails (a voice post must never cause a duplicate or undo a decision):
* a track already in the playlist, or already tracked with a non-failed status
  (incl. ``unsynced`` = deliberately removed), is reported but not re-added;
* work is serialised (one recognition at a time) and every step is best-effort —
  an exception here can never break the bot's other handlers;
* nothing is posted/forwarded to "look" at the message; only the bot's own reply.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import tempfile
from pathlib import Path

import structlog

logger = structlog.get_logger()

MAX_VOICE_BYTES = 20 * 1024 * 1024  # Bot API getFile cap
RECOGNIZE_TIMEOUT_S = 60


async def recognize(path: str) -> tuple[str, str] | None:
    """Return ``(artist, title)`` for the audio at *path*, or None if no match."""
    from shazamio import Shazam  # lazy: keeps the bot importable without the dep

    out = await asyncio.wait_for(Shazam().recognize(path), timeout=RECOGNIZE_TIMEOUT_S)
    track = out.get("track") if isinstance(out, dict) else None
    if not track or not track.get("title"):
        return None
    return (track.get("subtitle") or "").strip(), track["title"].strip()


class VoiceIdentifier:
    def __init__(self, bot, yt_client, track_repo, sync_log, channel_id: int) -> None:
        self._bot = bot  # telegram.Bot (for get_file)
        self._yt = yt_client
        self._tracks = track_repo
        self._log = sync_log
        self._channel_id = channel_id
        self._lock = asyncio.Semaphore(1)

    async def handle(self, update, context) -> None:
        message = update.channel_post
        if (
            message is None
            or message.chat_id != self._channel_id
            or message.voice is None
            or self._yt is None
        ):
            return
        voice = message.voice
        if voice.file_size and voice.file_size > MAX_VOICE_BYTES:
            logger.info("voice_id_too_big", size=voice.file_size)
            return
        async with self._lock:
            try:
                await self._process(message, context)
            except Exception:
                logger.warning("voice_id_failed", exc_info=True)

    async def _reply(self, message, text: str) -> None:
        with contextlib.suppress(Exception):
            await message.reply_text(text, disable_web_page_preview=True)

    async def _process(self, message, context) -> None:
        tmp = tempfile.mkdtemp(prefix="navaar_voice_")
        try:
            tg_file = await context.bot.get_file(message.voice.file_id)
            path = str(Path(tmp) / "voice.ogg")
            await tg_file.download_to_drive(path)

            ident = await recognize(path)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        if ident is None:
            await self._reply(message, "\U0001f399 Couldn't identify this one.")
            return
        artist, title = ident

        # Clip length says nothing about the full song, so no duration check.
        match = await asyncio.to_thread(self._yt.find_best_match, artist, title, None)
        if not match:
            await self._reply(
                message, f"\U0001f399 Heard {artist} — {title}, but found no YouTube match."
            )
            return
        vid = match["videoId"]
        label = f"{artist} — {title}".strip(" —")

        existing = await self._tracks.get_track_by_yt_video_id(vid)
        playlist = await asyncio.to_thread(self._yt.get_playlist_tracks)
        in_playlist = self._yt.is_in_playlist(vid, playlist)
        if in_playlist or (existing is not None and existing.status != "failed"):
            reason = "already in the playlist" if in_playlist else f"already tracked ({existing.status})"
            await self._log.log(
                "voice_identified_skipped",
                details={"artist": artist, "title": title, "video_id": vid, "reason": reason},
            )
            await self._reply(message, f"\U0001f399 {label}\nSkipped — {reason}.")
            return

        await asyncio.to_thread(self._yt.add_to_playlist, vid)
        await self._log.log(
            "voice_identified_added",
            details={"artist": artist, "title": title, "video_id": vid,
                     "message_id": message.message_id},
        )
        await self._reply(
            message,
            f"\U0001f399 {label}\n✅ Added to the playlist — it'll show up here shortly.\n"
            f"https://music.youtube.com/watch?v={vid}",
        )
