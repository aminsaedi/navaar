from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import structlog
from tenacity import retry, stop_after_attempt, wait_exponential

logger = structlog.get_logger()


class YTDownloader:
    def __init__(self, download_dir: str | None = None, cookies_file: str = "") -> None:
        self._download_dir = download_dir or tempfile.mkdtemp(prefix="navaar_")
        self._cookies_file = cookies_file
        Path(self._download_dir).mkdir(parents=True, exist_ok=True)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=30))
    async def download(self, video_id: str) -> str:
        url = f"https://music.youtube.com/watch?v={video_id}"
        output_template = str(Path(self._download_dir) / f"{video_id}.%(ext)s")
        cmd = [
            "yt-dlp",
            "--extract-audio",
            "--audio-format", "mp3",
            "--audio-quality", "0",
            "--embed-thumbnail",
            "--add-metadata",
            "--output", output_template,
            "--no-playlist",
            "--quiet",
            "--js-runtimes", "nodejs",
            url,
        ]
        if self._cookies_file and Path(self._cookies_file).exists():
            cmd.insert(-1, "--cookies")
            cmd.insert(-1, self._cookies_file)

        logger.info("yt_download_start", video_id=video_id)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            error = stderr.decode().strip()
            logger.error("yt_download_failed", video_id=video_id, error=error)
            raise RuntimeError(f"yt-dlp failed for {video_id}: {error}")

        # Find the output file
        output_path = Path(self._download_dir) / f"{video_id}.mp3"
        if not output_path.exists():
            # yt-dlp might have used a different extension before converting
            candidates = list(Path(self._download_dir).glob(f"{video_id}.*"))
            if candidates:
                output_path = candidates[0]
            else:
                raise FileNotFoundError(f"Downloaded file not found for {video_id}")

        logger.info("yt_download_complete", video_id=video_id, path=str(output_path))
        return str(output_path)

    def cleanup(self, file_path: str) -> None:
        try:
            Path(file_path).unlink(missing_ok=True)
        except OSError:
            logger.debug("cleanup_failed", path=file_path, exc_info=True)
