from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import structlog
from mutagen.id3 import ID3, TIT2, TPE1, ID3NoHeaderError

logger = structlog.get_logger()

# Cover art is downscaled to a square baseline JPEG: large or progressive JPEGs
# are a classic reason car head units skip a file's artwork (or the whole file).
COVER_SIZE = 500
NAVAAR_ID_TAG = "NAVAAR_ID"


class AudioError(RuntimeError):
    """ffmpeg/ffprobe could not produce a library file."""


def _nonempty(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 0


async def _run(*cmd: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


async def audio_codec(path: Path) -> str | None:
    """Codec name of the first audio stream ('mp3', 'aac', 'opus', ...)."""
    rc, out, _ = await _run(
        "ffprobe", "-v", "error", "-select_streams", "a:0",
        "-show_entries", "stream=codec_name", "-of", "default=nw=1:nk=1", str(path),
    )
    codec = out.strip().splitlines()[0] if rc == 0 and out.strip() else None
    return codec


async def _square_jpeg(src: Path, dest: Path, *, from_audio: bool) -> Path | None:
    """Center-crop to a square, cap at COVER_SIZE, write a baseline JPEG."""
    vf = (
        "crop='min(iw,ih)':'min(iw,ih)',"
        f"scale='min({COVER_SIZE},iw)':'min({COVER_SIZE},ih)',format=yuvj420p"
    )
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(src)]
    if from_audio:
        cmd += ["-an", "-map", "0:v:0"]
    cmd += ["-frames:v", "1", "-vf", vf, "-q:v", "3", str(dest)]
    rc, _, _ = await _run(*cmd)
    return dest if rc == 0 and _nonempty(dest) else None


async def extract_cover(audio: Path, work_dir: Path) -> Path | None:
    """The file's own embedded artwork (Telegram uploads and yt-dlp both embed it)."""
    return await _square_jpeg(audio, work_dir / "cover.jpg", from_audio=True)


async def fetch_youtube_cover(video_id: str, work_dir: Path) -> Path | None:
    """YouTube thumbnail for the track, as a square JPEG. Best-effort: None on any failure."""
    raw = work_dir / "thumb_raw.jpg"
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            for variant in ("maxresdefault", "hqdefault"):
                resp = await client.get(f"https://i.ytimg.com/vi/{video_id}/{variant}.jpg")
                if resp.status_code == 200 and resp.content:
                    raw.write_bytes(resp.content)
                    break
            else:
                return None
    except httpx.HTTPError:
        logger.info("library_cover_fetch_failed", video_id=video_id, exc_info=True)
        return None
    return await _square_jpeg(raw, work_dir / "cover.jpg", from_audio=False)


async def build_mp3(
    src: Path,
    dest: Path,
    *,
    title: str,
    artist: str | None,
    album: str,
    navaar_id: str,
    cover: Path | None,
    bitrate_kbps: int,
) -> None:
    """Write ``dest`` as a car-player-friendly MP3.

    - MP3 sources are stream-copied (no generation loss); anything else is
      encoded to CBR ``bitrate_kbps`` with LAME.
    - All source tags are dropped and rewritten as ID3v2.3 (+ID3v1): v2.4 is not
      understood by many head units.
    - ``navaar_id`` is stored as TXXX:NAVAAR_ID so a file can be matched back to
      its track even if the library manifest is lost.
    """
    codec = await audio_codec(src)
    if codec is None:
        raise AudioError(f"no audio stream in {src.name}")

    cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(src)]
    if cover is not None:
        cmd += ["-i", str(cover)]
    cmd += ["-map", "0:a:0"]
    if cover is not None:
        cmd += [
            "-map", "1:v:0", "-c:v", "copy", "-disposition:v:0", "attached_pic",
            "-metadata:s:v", "title=Album cover", "-metadata:s:v", "comment=Cover (front)",
        ]
    if codec == "mp3":
        cmd += ["-c:a", "copy"]
    else:
        cmd += ["-c:a", "libmp3lame", "-b:a", f"{bitrate_kbps}k"]
    cmd += [
        "-map_metadata", "-1", "-map_chapters", "-1",
        "-id3v2_version", "3", "-write_id3v1", "1",
        "-metadata", f"title={title}",
        "-metadata", f"album={album}",
        "-metadata", "album_artist=Various Artists",
        "-metadata", "compilation=1",
        "-metadata", f"{NAVAAR_ID_TAG}={navaar_id}",
    ]
    if artist:
        cmd += ["-metadata", f"artist={artist}"]
    cmd += ["-f", "mp3", str(dest)]

    rc, _, err = await _run(*cmd)
    if rc != 0 or not _nonempty(dest):
        raise AudioError(f"ffmpeg failed ({codec}): {err.strip()[-300:]}")


def read_navaar_id(path: Path) -> str | None:
    """TXXX:NAVAAR_ID of a library file, or None if absent/unreadable."""
    try:
        tags = ID3(path)
    except (ID3NoHeaderError, OSError, Exception):  # mutagen raises assorted errors on junk
        return None
    frame = tags.get(f"TXXX:{NAVAAR_ID_TAG}")
    return str(frame.text[0]) if frame and frame.text else None


def retag(path: Path, *, title: str, artist: str | None) -> None:
    """Rewrite title/artist in place (ID3v2.3) — no re-download, no re-encode."""
    tags = ID3(path)
    tags.setall("TIT2", [TIT2(encoding=1, text=title)])
    if artist:
        tags.setall("TPE1", [TPE1(encoding=1, text=artist)])
    else:
        tags.delall("TPE1")
    tags.save(path, v2_version=3, v1=2)
