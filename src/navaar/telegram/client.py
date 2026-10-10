from __future__ import annotations

import tempfile
from pathlib import Path

import structlog
from telegram import Bot
from telegram.error import NetworkError, TimedOut
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


def _is_connect_error(exc: BaseException) -> bool:
    """True for errors that mean the connection was never established.

    A ConnectError guarantees the upload never started, so it is safe to retry
    without risk of posting a duplicate. TimedOut is explicitly excluded: a
    write/read timeout can fire after Telegram has already accepted the file,
    so retrying would create a duplicate in the channel.
    """
    if isinstance(exc, TimedOut):
        return False
    if isinstance(exc, NetworkError):
        msg = str(exc).lower()
        return any(k in msg for k in ("connect", "connection refused", "connection reset"))
    return False


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
        """Send audio to channel.

        ConnectErrors (connection never established) are retried up to 3 times —
        they are safe because the upload provably never started. TimedOut is NOT
        retried: a write/read timeout can fire after Telegram accepted the bytes,
        so a retry would post a duplicate in the channel.
        """
        with open(file_path, "rb") as f:
            for attempt in range(3):
                try:
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
                    break
                except Exception as exc:
                    if _is_connect_error(exc) and attempt < 2:
                        logger.warning(
                            "tg_send_audio_connect_retry",
                            attempt=attempt + 1,
                            error=str(exc),
                        )
                        continue
                    raise
        logger.info(
            "tg_audio_sent",
            message_id=message.message_id,
            title=title,
            performer=performer,
        )
        return message.message_id

    async def delete_message(self, message_id: int) -> None:
        await self._bot.delete_message(chat_id=self._channel_id, message_id=message_id)

    def cleanup(self, file_path: str) -> None:
        try:
            Path(file_path).unlink(missing_ok=True)
        except OSError:
            logger.debug("cleanup_failed", path=file_path, exc_info=True)
