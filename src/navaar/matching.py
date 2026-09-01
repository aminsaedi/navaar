from __future__ import annotations

import re
import unicodedata

# Two recordings of the same song differ in length (live versions, remasters, an
# added intro), so the ratio has to be generous: "Shabe Meykhooneh" arrived as a
# 26-minute live take whose studio counterpart is a few minutes long, and that is
# a *correct* match. Beyond this factor the candidate is a different work.
_MAX_DURATION_RATIO = 4.0

_NON_ALNUM = re.compile(r"[^\w\s]", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")
# The YouTube Data API returns titles HTML-escaped ('&quot;Yar&quot;').
_ENTITIES = {"&quot;": " ", "&amp;": " ", "&#39;": " ", "&apos;": " ", "&lt;": " ", "&gt;": " "}


def normalize_title(text: str | None) -> str:
    """Casefold and strip punctuation so titles survive the decoration the
    services add around them."""
    if not text:
        return ""
    for entity, replacement in _ENTITIES.items():
        text = text.replace(entity, replacement)
    text = unicodedata.normalize("NFKC", text)
    text = _NON_ALNUM.sub(" ", text.casefold())
    return _WHITESPACE.sub(" ", text).strip()


def titles_overlap(source: str | None, candidate: str | None) -> bool:
    """True when one normalized title contains the other.

    Containment rather than similarity, deliberately: correct YouTube matches
    routinely score *low* on similarity because the uploader's title carries the
    artist and a Persian rendering of the name — "Moein - Parvardegara معین
    پروردگارا" is the right hit for "Parvardegar". A similarity threshold tuned
    to reject the bad matches would throw those away too.
    """
    a, b = normalize_title(source), normalize_title(candidate)
    if not a or not b:
        return False
    return a in b or b in a


def duration_plausible(source_seconds: int | None, candidate_seconds: int | None) -> bool:
    """True unless the durations are too far apart to be the same work.

    An unknown duration on either side is absence of evidence, not evidence of a
    bad match, so it passes.
    """
    if not source_seconds or not candidate_seconds:
        return True
    longer = max(source_seconds, candidate_seconds)
    shorter = min(source_seconds, candidate_seconds)
    return longer <= shorter * _MAX_DURATION_RATIO


def is_plausible_match(
    source_title: str | None,
    source_seconds: int | None,
    candidate_title: str | None,
    candidate_seconds: int | None,
) -> bool:
    """Reject only the clearly-wrong: a candidate that neither shares the source's
    title nor comes near its length.

    Search returns the top hit for *any* query, so an item that simply is not on
    the service still yields a confident-looking result — a 51-minute radio
    programme was matched to an unrelated 5-minute song and silently added to
    both playlists. Requiring only one of the two signals keeps long live
    versions (exact title, very different length) and artist-prefixed uploads
    (right length, decorated title) working, while that case fails both.
    """
    return titles_overlap(source_title, candidate_title) or duration_plausible(
        source_seconds, candidate_seconds
    )


_ISO_DURATION = re.compile(
    r"^P(?:(?P<days>\d+)D)?T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?$"
)


def parse_iso8601_duration(value: str | None) -> int | None:
    """Seconds from the ISO-8601 duration the YouTube Data API reports
    ("PT4M13S"). None when absent or unparseable (live streams report "P0D")."""
    if not value:
        return None
    match = _ISO_DURATION.match(value)
    if not match:
        return None
    parts = {k: int(v) for k, v in match.groupdict(default="0").items()}
    total = (
        parts["days"] * 86400
        + parts["hours"] * 3600
        + parts["minutes"] * 60
        + parts["seconds"]
    )
    return total or None
