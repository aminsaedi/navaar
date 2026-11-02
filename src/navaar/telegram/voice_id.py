"""Identify voice messages posted in the channel (Shazam) and add the best hit.

Anyone who can post in the channel can record a voice message of a song they
heard. For every channel voice message we:

1. download it (Bot API getFile — read-only, 20 MB cap),
2. transcode it to WAV (shazamio's decoder rejects Telegram's OGG/Opus) and
   ask Shazam (``shazamio``, unofficial but keyless) what it is — retrying with
   the clip looped when a short recording yields no fingerprint match,
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
import math
import os
import shutil
import tempfile
from pathlib import Path

import structlog

logger = structlog.get_logger()

MAX_VOICE_BYTES = 20 * 1024 * 1024  # Bot API getFile cap
RECOGNIZE_TIMEOUT_S = 60
# A Telegram voice note is often only 5-10 s of a quiet/ambient recording, which
# is too little for Shazam's fingerprint engine and comes back with zero matches.
# When the direct pass finds nothing, retry with the clip looped up to this many
# seconds before giving up.
RECOGNIZE_RETRY_SECONDS = 30


def _nonempty(path: Path) -> bool:
    return os.path.isfile(path) and os.path.getsize(path) > 0


async def _to_wav(src: Path, dest: Path, *, loops: int = 1) -> bool:
    """Decode *src* to canonical mono PCM WAV with ffmpeg.

    shazamio's Rust core (symphonia) cannot decode Telegram's OGG/Opus voice
    container — it raises ``SignatureError: unexpected end of file`` — so every
    voice is transcoded first. ``loops`` repeats the clip to give the fingerprint
    engine more frames on the retry pass.
    """
    args: list[str] = []
    if loops > 1:
        args += ["-stream_loop", str(loops - 1)]
    args += ["-i", str(src), "-ac", "1", "-ar", "44100", str(dest)]
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-v", "error", *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        logger.info("voice_id_transcode_failed", stderr=err.decode(errors="replace")[-500:])
        return False
    return await asyncio.to_thread(_nonempty, dest)


async def _duration(path: Path) -> float:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=nw=1:nk=1", str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    try:
        return float(out.decode().strip())
    except ValueError:
        return 0.0


async def _identify(shazam, path: Path) -> tuple[str, str] | None:
    out = await asyncio.wait_for(shazam.recognize(str(path)), timeout=RECOGNIZE_TIMEOUT_S)
    track = out.get("track") if isinstance(out, dict) else None
    if not track or not track.get("title"):
        return None
    return (track.get("subtitle") or "").strip(), track["title"].strip()


async def recognize(path: str) -> tuple[str, str] | None:
    """Return ``(artist, title)`` for the audio at *path*, or None if no match."""
    from shazamio import Shazam  # lazy: keeps the bot importable without the dep

    src = Path(path)
    shazam = Shazam()
    try:
        wav = src.with_name("voice.wav")
        if not await _to_wav(src, wav):
            return None
        ident = await _identify(shazam, wav)
        if ident is not None:
            return ident

        seconds = await _duration(wav)
        # A clip already long enough had plenty of frames on the first pass —
        # repeating the same audio cannot reveal anything new. Short clips get
        # looped up to ~30 s (capped so a 2 s blip does not become a huge file).
        if seconds >= RECOGNIZE_RETRY_SECONDS:
            return None
        loops = min(6, max(2, math.ceil(RECOGNIZE_RETRY_SECONDS / seconds) if seconds > 0 else 3))
        looped = src.with_name("voice_loop.wav")
        if await _to_wav(src, looped, loops=loops):
            return await _identify(shazam, looped)
        return None
    finally:
        with contextlib.suppress(Exception):
            await shazam.close()


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
                # Never leave the listener without an answer: the first deploy of
                # this feature swallowed a Shazam decode error and posted nothing.
                await self._reply(
                    message,
                    "\U0001f399 Couldn't identify this one right now — please try again.",
                )

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
