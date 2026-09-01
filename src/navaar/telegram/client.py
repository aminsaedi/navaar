from __future__ import annotations

import tempfile
from pathlib import Path

import structlog
from telegram import Bot
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from navaar.metrics import TG_DOWNLOAD_TOTAL

logger = structlog.get_logger()


class FileTooBigError(Exception):
    """The audio exceeds the Bot API's 20 MB getFile download cap.

    Permanent for a given file: the bot can never fetch the bytes, no matter how
    many times it asks. Callers should degrade (identify from the message
    metadata) rather than fail the track.
    """


def _is_file_too_big(exc: BaseException) -> bool:
    # python-telegram-bot surfaces this as BadRequest("File is too big").
    return "file is too big" in str(exc).lower()


# Retry transient network faults, but never the 20 MB cap — it is a property of
# the file, so the two extra attempts only add ~30s of backoff before the same
# failure.
_retry_downloads = retry_if_exception(lambda e: not _is_file_too_big(e))


class TelegramClient:
    def __init__(self, bot: Bot, channel_id: int) -> None:
        self._bot = bot
        self._channel_id = channel_id

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(min=2, max=30),
        retry=_retry_downloads,
    )
    async def download_file(self, file_id: str, dest_dir: str | None = None) -> str:
        dest_dir = dest_dir or tempfile.mkdtemp(prefix="navaar_tg_")
        Path(dest_dir).mkdir(parents=True, exist_ok=True)

        try:
            tg_file = await self._bot.get_file(file_id)
            file_name = tg_file.file_path.split("/")[-1] if tg_file.file_path else f"{file_id}.mp3"
            local_path = str(Path(dest_dir) / file_name)
            await tg_file.download_to_drive(local_path)
            TG_DOWNLOAD_TOTAL.labels(result="success").inc()
        except Exception as exc:
            TG_DOWNLOAD_TOTAL.labels(result="failure").inc()
            if _is_file_too_big(exc):
                raise FileTooBigError(str(exc)) from exc
            raise

        logger.info("tg_file_downloaded", file_id=file_id, path=local_path)
        return local_path

    async def send_audio(
        self,
        file_path: str,
        title: str | None = None,
        performer: str | None = None,
        duration: int | None = None,
        caption: str | None = None,
    ) -> int:
        """Send audio to channel. No retry — a timeout likely means the upload
        already went through, and retrying would create duplicates."""
        with open(file_path, "rb") as f:
            message = await self._bot.send_audio(
                chat_id=self._channel_id,
                audio=f,
                title=title,
                performer=performer,
                duration=duration,
                caption=caption,
                read_timeout=300,
                write_timeout=300,
                connect_timeout=30,
            )
        logger.info(
            "tg_audio_sent",
            message_id=message.message_id,
            title=title,
            performer=performer,
        )
        return message.message_id

    def cleanup(self, file_path: str) -> None:
        try:
            Path(file_path).unlink(missing_ok=True)
        except OSError:
            logger.debug("cleanup_failed", path=file_path, exc_info=True)
