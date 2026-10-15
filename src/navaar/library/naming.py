from __future__ import annotations

import re
import unicodedata

# Characters FAT32 (and therefore every car head unit) refuses in a file name,
# plus ASCII control characters. Unicode letters (Persian titles) are fine: FAT32
# long file names are UTF-16.
_FAT_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_SPACES = re.compile(r"\s+")

# Keep names short: FAT32 allows 255 UTF-16 units, but many head units truncate or
# choke on long paths, and the USB copy lives under /navaar/.
MAX_STEM_CHARS = 100


# Upload noise in YouTube video titles (yt->tg rows store the raw video title).
# Only bracketed, clearly non-musical tags: "(Live)" / "(Remastered)" are kept.
_TITLE_NOISE = re.compile(
    r"\s*[(\[]\s*(?:official\b[^)\]]*|(?:music\s+|lyrics?\s+)?video(?:\s+clip)?|"
    r"lyrics?|audio|visuali[sz]er|hd|hq|4k|clip)\s*[)\]]",
    re.IGNORECASE,
)
_ARTIST_PREFIX_SEP = re.compile(r"^\s*[-\u2013\u2014:|]\s*")


def clean_title(artist: str | None, title: str | None) -> str:
    """Title as it should appear on a car screen: without a repeated
    'Artist - ' prefix and without '(Official Music Video)'-style noise."""
    text = (title or "").strip()
    if artist:
        a = artist.strip()
        if a and text.casefold().startswith(a.casefold()):
            rest = text[len(a):]
            m = _ARTIST_PREFIX_SEP.match(rest)
            if m and rest[m.end():].strip():
                text = rest[m.end():]
    text = _TITLE_NOISE.sub("", text)
    text = _SPACES.sub(" ", text).strip(" -\u2013\u2014")
    return text or (title or "").strip() or "Unknown"


def track_stem(artist: str | None, title: str | None) -> str:
    """'Artist - Title' as a FAT32-safe file stem (no extension)."""
    title = (title or "").strip() or "Unknown"
    artist = (artist or "").strip()
    raw = f"{artist} - {title}" if artist else title
    raw = unicodedata.normalize("NFC", raw)
    clean = _FAT_FORBIDDEN.sub(" ", raw)
    clean = _SPACES.sub(" ", clean).strip()
    clean = clean[:MAX_STEM_CHARS].rstrip()
    # FAT strips trailing dots/spaces, which would make two names collide on disk.
    clean = clean.rstrip(". ")
    return clean or "Unknown"


def dedupe_key(artist: str | None, title: str | None) -> str:
    """Case/spacing-insensitive identity used to drop the same song posted twice."""
    return track_stem(artist, title).casefold()


def unique_file_name(stem: str, taken: set[str], ext: str = ".mp3") -> str:
    """Return '<stem><ext>', or '<stem> (2)<ext>' etc. when the name is taken.

    Comparison is case-insensitive because FAT32 is."""
    lowered = {t.casefold() for t in taken}
    name = f"{stem}{ext}"
    n = 2
    while name.casefold() in lowered:
        name = f"{stem} ({n}){ext}"
        n += 1
    return name
