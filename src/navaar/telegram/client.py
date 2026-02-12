from __future__ import annotations

import tempfile
from pathlib import Path

import structlog
from tenacity import retry, stop_after_attempt, wait_exponential
from telegram import Bot

from navaar.metrics import TG_DOWNLOAD_TOTAL

logger = structlog.get_logger()


class TelegramClient:
    def __init__(self, bot: Bot, channel_id: int) -> None:
        self._bot = bot
        self._channel_id = channel_id

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=30))
    async def download_file(self, file_id: str, dest_dir: str | None = None) -> str:
        dest_dir = dest_dir or tempfile.mkdtemp(prefix="navaar_tg_")
        Path(dest_dir).mkdir(parents=True, exist_ok=True)

        try:
            tg_file = await self._bot.get_file(file_id)
            file_name = tg_file.file_path.split("/")[-1] if tg_file.file_path else f"{file_id}.mp3"
            local_path = str(Path(dest_dir) / file_name)
            await tg_file.download_to_drive(local_path)
            TG_DOWNLOAD_TOTAL.labels(result="success").inc()
        except Exception:
            TG_DOWNLOAD_TOTAL.labels(result="failure").inc()
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
