from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import structlog
from mutagen import File as MutagenFile
from mutagen.id3 import ID3

logger = structlog.get_logger()


@dataclass
class TrackInfo:
    artist: str | None
    title: str
    method: str  # id3, tg_metadata, filename


def identify_from_id3(file_path: str) -> TrackInfo | None:
    try:
        audio = MutagenFile(file_path)
        if audio is None:
            return None

        if hasattr(audio, "tags") and audio.tags is not None:
            tags = audio.tags
            # Try ID3 tags
            if isinstance(tags, ID3):
                title = tags.get("TIT2")
                artist = tags.get("TPE1")
                if title:
                    return TrackInfo(
                        artist=str(artist) if artist else None,
                        title=str(title),
                        method="id3",
                    )

            # Try generic tag interface (MP4, Vorbis, etc.)
            title = None
            artist = None
            for title_key in ("title", "\xa9nam"):
                if title_key in tags:
                    val = tags[title_key]
                    title = val[0] if isinstance(val, list) else str(val)
                    break
            for artist_key in ("artist", "\xa9ART"):
                if artist_key in tags:
                    val = tags[artist_key]
                    artist = val[0] if isinstance(val, list) else str(val)
                    break
            if title:
                return TrackInfo(artist=artist, title=title, method="id3")
    except Exception:
        logger.debug("id3_parse_failed", file_path=file_path, exc_info=True)
    return None


def identify_from_tg_metadata(
    performer: str | None, title: str | None
) -> TrackInfo | None:
    if title:
        return TrackInfo(artist=performer, title=title, method="tg_metadata")
    return None


_SEPARATORS = re.compile(r"\s*[-–—]\s*")


def identify_from_filename(file_name: str | None) -> TrackInfo | None:
    if not file_name:
        return None
    stem = Path(file_name).stem
    # Clean up common patterns
    stem = re.sub(r"\(Official.*?\)", "", stem, flags=re.IGNORECASE).strip()
    stem = re.sub(r"\[.*?\]", "", stem).strip()

    parts = _SEPARATORS.split(stem, maxsplit=1)
    if len(parts) == 2:
        artist, title = parts[0].strip(), parts[1].strip()
        if artist and title:
            return TrackInfo(artist=artist, title=title, method="filename")

    # No separator: use whole stem as title
    if stem:
        return TrackInfo(artist=None, title=stem, method="filename")
    return None


def identify_track(
    file_path: str | None = None,
    tg_performer: str | None = None,
    tg_title: str | None = None,
    file_name: str | None = None,
) -> TrackInfo | None:
    """Run the identification pipeline: ID3 → TG metadata → filename."""
    if file_path:
        result = identify_from_id3(file_path)
        if result:
            logger.info("track_identified", method="id3", title=result.title, artist=result.artist)
            return result

    result = identify_from_tg_metadata(tg_performer, tg_title)
    if result:
        logger.info("track_identified", method="tg_metadata", title=result.title, artist=result.artist)
        return result

    result = identify_from_filename(file_name)
    if result:
        logger.info("track_identified", method="filename", title=result.title, artist=result.artist)
        return result

    logger.warning("track_identification_failed", file_name=file_name)
    return None
