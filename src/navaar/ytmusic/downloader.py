from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

import structlog
from tenacity import retry, stop_after_attempt, wait_exponential

logger = structlog.get_logger()

_MB = 1024 * 1024

# When a downloaded file exceeds the upload limit, re-encode it to a lower mp3
# bitrate that fits. Aim a little under the limit to leave room for container and
# multipart overhead, and clamp the bitrate to a sane range.
_TARGET_HEADROOM = 0.94
_MIN_BITRATE_KBPS = 64
_MAX_BITRATE_KBPS = 192


def _target_bitrate_kbps(duration_sec: float, max_bytes: int) -> int:
    """Pick an mp3 bitrate (kbps) that keeps a file of the given duration under
    ``max_bytes``, clamped to [_MIN, _MAX]. Falls back to the floor when the
    duration is unknown — a too-low bitrate that uploads beats a too-high one
    that gets rejected."""
    if duration_sec <= 0:
        return _MIN_BITRATE_KBPS
    kbps = int((max_bytes * 8 * _TARGET_HEADROOM) / duration_sec / 1000)
    return max(_MIN_BITRATE_KBPS, min(_MAX_BITRATE_KBPS, kbps))


class YTDownloader:
    def __init__(
        self,
        download_dir: str | None = None,
        cookies_file: str = "",
        max_upload_mb: int = 50,
    ) -> None:
        self._download_dir = download_dir or tempfile.mkdtemp(prefix="navaar_")
        self._cookies_file = cookies_file
        self._max_upload_bytes = max_upload_mb * _MB
        Path(self._download_dir).mkdir(parents=True, exist_ok=True)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=30))
    async def download(self, video_id: str) -> str:
        url = f"https://music.youtube.com/watch?v={video_id}"
        output_template = str(Path(self._download_dir) / f"{video_id}.%(ext)s")
        # Invoke yt-dlp through the running interpreter (`python -m yt_dlp`) rather
        # than the bare `yt-dlp` console script: the latter only resolves when the
        # venv's bin dir is on PATH, which it isn't when the app is launched via the
        # venv python directly (as in the container) instead of `uv run`. Using
        # sys.executable makes downloads work regardless of how the app is started.
        cmd = [
            sys.executable, "-m", "yt_dlp",
            "--extract-audio",
            "--audio-format", "mp3",
            "--audio-quality", "0",
            "--embed-thumbnail",
            "--add-metadata",
            "--output", output_template,
            "--no-playlist",
            "--quiet",
            "--js-runtimes", "node",
            "--remote-components", "ejs:github",
        ]

        if self._cookies_file and Path(self._cookies_file).exists():
            cmd.extend(["--cookies", self._cookies_file])

        cmd.append(url)

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

        output_path = await self._fit_upload_limit(output_path)

        logger.info("yt_download_complete", video_id=video_id, path=str(output_path))
        return str(output_path)

    async def _fit_upload_limit(self, path: Path) -> Path:
        """If the file is over the upload limit, re-encode it to a lower mp3
        bitrate that fits and return the new path (deleting the oversized
        original). Returns the path unchanged when it already fits or when
        re-encoding fails (the upload then fails loudly rather than silently)."""
        try:
            size = path.stat().st_size
        except OSError:
            return path
        if size <= self._max_upload_bytes:
            return path

        duration = await self._probe_duration(path)
        kbps = _target_bitrate_kbps(duration, self._max_upload_bytes)
        compressed = path.with_name(f"{path.stem}.{kbps}k.mp3")
        logger.info(
            "audio_compress_start",
            path=str(path), size_bytes=size, duration_sec=duration, target_kbps=kbps,
        )
        cmd = [
            "ffmpeg", "-y", "-i", str(path),
            "-map", "0:a:0",          # audio only; drop any embedded cover stream
            "-map_metadata", "0",     # keep title/artist tags
            "-c:a", "libmp3lame",
            "-b:a", f"{kbps}k",
            str(compressed),
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0 or not compressed.exists():
            logger.error(
                "audio_compress_failed",
                path=str(path), error=stderr.decode().strip()[-400:],
            )
            return path

        new_size = compressed.stat().st_size
        path.unlink(missing_ok=True)
        level = logger.info if new_size <= self._max_upload_bytes else logger.warning
        level(
            "audio_compressed" if new_size <= self._max_upload_bytes else "audio_still_oversize",
            path=str(compressed), old_size=size, new_size=new_size, target_kbps=kbps,
        )
        return compressed

    async def _probe_duration(self, path: Path) -> float:
        """Return the media duration in seconds via ffprobe, or 0.0 on failure."""
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            return float(stdout.decode().strip())
        except (ValueError, OSError):
            return 0.0

    def cleanup(self, file_path: str) -> None:
        try:
            Path(file_path).unlink(missing_ok=True)
        except OSError:
            logger.debug("cleanup_failed", path=file_path, exc_info=True)
