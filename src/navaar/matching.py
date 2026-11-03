from __future__ import annotations

import difflib
import re
import unicodedata

# Matching policy: a candidate must agree on song title AND artist, and (when
# both are known) be close enough in length to be the *same recording*. The old
# policy accepted "title matches OR duration is within 4x", which let a same-title
# cover by another artist through, and — because yt→sp stored no duration at all —
# let any popular search hit take the slot.

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


# Words that decorate a title (quality/format/credit noise) but do not identify
# the song. Dropped before comparing titles.
_TITLE_NOISE = {
    "official", "video", "audio", "lyric", "lyrics", "lyricvideo", "musicvideo",
    "music", "topic", "hd", "hq", "4k", "8k", "mv", "full", "song", "clip",
    "visualizer", "visualiser", "original", "new", "quality", "explicit",
    "clean", "prod", "by", "feat", "ft", "featuring", "with", "vevo",
}
# Short connective words. A one/two-letter difference here ("wi"/"we") is a
# transliteration artifact, not a different title, so they carry no signal.
_CONNECTORS = {
    "a", "an", "the", "o", "of", "and", "we", "wi", "wa", "ya", "ye", "de",
    "la", "el", "al", "on", "in", "to", "for", "from", "is", "it", "my", "me",
    "i", "be", "do",
}
# Markers of a *different recording* of the same song. Kept out of the title
# comparison but remembered: a version mismatch must be paid for by an exact
# duration match.
_VERSION_MARKERS = {
    "live", "remix", "remaster", "remastered", "acoustic", "instrumental",
    "demo", "cover", "edit", "extended", "version", "mix", "slowed", "reverb",
    "nightcore", "8d",
}
# Generic words in uploader/channel names that say nothing about the artist.
_GENERIC_ARTIST = {
    "topic", "vevo", "music", "records", "record", "group", "band", "official",
    "channel", "tv", "the", "and", "entertainment", "production", "productions",
    "media", "label", "audio", "video",
}


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _tok_eq(a: str, b: str) -> bool:
    """Token equality tolerant of transliteration, but never of numbers.

    A digit is part of the identity ("...Karde 2" is not "...Karde"); longer
    words may differ by one character ("Viguen"/"Vigen", "Parvardegar"/
    "Parvardegara"). Short words must match exactly so "Gol" cannot absorb
    "Golbarg".
    """
    if a == b:
        return True
    if a.isdigit() or b.isdigit():
        return False
    shortest = min(len(a), len(b))
    if shortest < 4:
        return False
    return _levenshtein(a, b) <= (2 if shortest >= 8 else 1)


def _tokens(text: str | None) -> list[str]:
    return normalize_title(text).split()


def _artist_tokens(value: str | list[str] | None) -> list[str]:
    values = [value] if isinstance(value, str) else list(value or [])
    out: list[str] = []
    for v in values:
        out.extend(t for t in _tokens(v) if t not in _GENERIC_ARTIST)
    return out


def _title_parts(
    text: str | None, remove: list[str] | None = None
) -> tuple[list[str], set[str]]:
    """Core title tokens plus any version markers found in the title."""
    remove = remove or []
    core: list[str] = []
    flags: set[str] = set()
    for token in _tokens(text):
        if token in _TITLE_NOISE or token in _CONNECTORS:
            continue
        if any(_tok_eq(token, r) for r in remove):
            continue
        if token in _VERSION_MARKERS:
            flags.add(token)
            continue
        core.append(token)
    return core, flags


def _contains(big: list[str], small: list[str]) -> bool:
    used: set[int] = set()
    for token in small:
        for i, other in enumerate(big):
            if i not in used and _tok_eq(token, other):
                used.add(i)
                break
        else:
            return False
    return True


def _jaccard(a: list[str], b: list[str]) -> float:
    if not a or not b:
        return 0.0
    matched = 0
    used: set[int] = set()
    for token in a:
        for i, other in enumerate(b):
            if i not in used and _tok_eq(token, other):
                used.add(i)
                matched += 1
                break
    union = len(a) + len(b) - matched
    return matched / union if union else 0.0


def _significant_numbers(tokens: list[str]) -> set[str]:
    # Track-part suffixes matter ("...Karde 2"); 4-digit years are release
    # metadata and should not disqualify a match.
    out: set[str] = set()
    for token in tokens:
        if token.isdigit() and not (len(token) == 4 and 1900 <= int(token) <= 2099):
            out.add(token)
    return out


def _char_similarity(a: list[str], b: list[str]) -> float:
    # Concatenate so hyphenation differences ("Faje-eh" / "Fajaeh") and
    # transliteration variants ("Bachehamoon"/"Bachehamoon") still line up.
    return difflib.SequenceMatcher(None, "".join(a), "".join(b)).ratio()


def _variant_texts(text: str | None) -> list[str]:
    """The title plus useful slices: without bracketed credits, the bracketed
    part itself, and either side of an 'Artist - Song' separator."""
    if not text:
        return [""]
    variants = [text]
    variants.append(re.sub(r"[\(\[].*?[\)\]]", " ", text))
    variants.extend(re.findall(r"[\(\[](.*?)[\)\]]", text))
    parts = re.split(r"\s+[-\u2013\u2014]\s+", text, maxsplit=1)
    if len(parts) == 2:
        variants.extend(parts)
    out: list[str] = []
    for variant in variants:
        variant = variant.strip()
        if variant and variant not in out:
            out.append(variant)
    return out


def _pair_title_score(
    a: list[str], b: list[str], a_flags: set[str], b_flags: set[str]
) -> float:
    if not a or not b:
        return 0.0
    if _significant_numbers(a) != _significant_numbers(b):
        return 0.0

    if _contains(a, b) and _contains(b, a):
        base = 1.0
    elif _contains(b, a) or _contains(a, b):
        small, big = (a, b) if _contains(b, a) else (b, a)
        extras = [t for t in big if not any(_tok_eq(t, s) for s in small)]
        penalty = 0.45 if any(t.isdigit() for t in extras) else 0.0
        base = max(0.15, 0.85 - penalty)
    else:
        similarity = _jaccard(a, b)
        base = similarity if similarity >= 0.6 else 0.0

    # Character-level fallback for transliteration/hyphenation variants. Only
    # used when the numbers agree (checked above), so "...Karde 2" cannot ride
    # its 95% character similarity past "...Karde".
    ratio = max(
        _char_similarity(x, y)
        for x in _script_variants(a)
        for y in _script_variants(b)
    )
    if ratio >= 0.88:
        base = max(base, 0.95)
    elif ratio >= 0.82:
        base = max(base, 0.85)
    elif ratio >= 0.75:
        base = max(base, 0.65)

    if a_flags != b_flags:
        base *= 0.75
    return base


def title_similarity(
    source: str | None,
    candidate: str | None,
    *,
    source_artist: str | None = None,
    candidate_artists: list[str] | None = None,
) -> float:
    """How strongly the *song* titles agree, in [0, 1].

    Token-based with a character-level fallback, so "Gol" does not match
    "Golbarg" while "Ghame Tanhaei"/"Gham-E Tanhaee" and "Faje-eh"/"Fajaeh" do.
    The artist prefix and format noise are stripped from both sides first.
    """
    remove_a = _artist_tokens(source_artist)
    remove_b = _artist_tokens(candidate_artists)
    a_variants = [_title_parts(v, remove_a) for v in _variant_texts(source)]
    b_variants = [_title_parts(v, remove_b) for v in _variant_texts(candidate)]
    best = 0.0
    for a, a_flags in a_variants:
        for b, b_flags in b_variants:
            best = max(best, _pair_title_score(a, b, a_flags, b_flags))
    return best


def titles_overlap(source: str | None, candidate: str | None) -> bool:
    """Backwards-compatible boolean form of :func:`title_similarity`."""
    return title_similarity(source, candidate) >= 0.5


def duration_plausible(source_seconds: int | None, candidate_seconds: int | None) -> bool:
    """Whether two durations could be the *same recording*.

    Tight on purpose: a cover, a live take or a radio edit is a different track
    even when the title matches. Unknown durations carry no evidence either way.
    """
    score = duration_score(source_seconds, candidate_seconds)
    return score is None or score > 0


def duration_score(source_seconds: int | None, candidate_seconds: int | None) -> float | None:
    """1.0 for a near-exact length, falling to 0 beyond 25%. None = unknown.

    YouTube uploads often add an intro/outro, so a same-recording match can be
    ~10-20% longer than the Spotify cut; a different version is usually much
    further apart than 25%.
    """
    if not source_seconds or not candidate_seconds:
        return None
    diff = abs(source_seconds - candidate_seconds)
    longest = max(source_seconds, candidate_seconds)
    if diff <= max(8, 0.03 * longest):
        return 1.0
    if diff <= max(15, 0.06 * longest):
        return 0.8
    if diff <= max(30, 0.12 * longest):
        return 0.5
    if diff <= 0.25 * longest:
        return 0.25
    return 0.0


def _script_variants(tokens: list[str]) -> list[list[str]]:
    """Token list plus its Latin-only and non-Latin-only subsets.

    Bilingual uploads put the same title in two scripts; comparing the Latin
    half of one title with the Latin half of the other avoids the translation
    diluting the similarity.
    """
    latin = [t for t in tokens if all(ord(c) < 128 for c in t)]
    other = [t for t in tokens if any(ord(c) >= 128 for c in t)]
    variants = [tokens]
    if latin and len(latin) != len(tokens):
        variants.append(latin)
    if other and len(other) != len(tokens):
        variants.append(other)
    return variants


def artist_evidence(
    source_artist: str | None,
    source_title: str | None,
    candidate_artists: list[str] | None,
    candidate_title: str | None = None,
) -> bool:
    """Is there evidence the candidate is by the same artist?

    Checked per candidate artist name so a featuring credit cannot drag the
    match in on a shared first name ("Mohsen Yeganeh" vs "Mohsen Chavoshi" must
    not pass on "mohsen"). The source's "artist" is sometimes an uploader
    channel, so the source title is searched too.
    """
    source_artist_tokens = _artist_tokens(source_artist)
    source_text_tokens = list(
        dict.fromkeys(source_artist_tokens + _tokens(source_title))
    )
    candidate_names = (
        [candidate_artists]
        if isinstance(candidate_artists, str)
        else list(candidate_artists or [])
    )
    if not source_artist_tokens and not candidate_names:
        return True  # no artist metadata on either side; title/duration decide

    for name in candidate_names:
        candidate_tokens = _artist_tokens(name)
        if not candidate_tokens:
            continue
        # All words of this artist name appear in the source artist/title.
        if _contains(source_text_tokens, candidate_tokens):
            return True
        # One name contains the other ("Andy" vs "Andy & Kouros").
        if source_artist_tokens and (
            _contains(candidate_tokens, source_artist_tokens)
            or _contains(source_artist_tokens, candidate_tokens)
        ):
            return True

    # The source artist appears in the candidate title (channel/uploader source).
    # Any one name is enough: "Bamdad & Tahrang" is still Bamdad's track when the
    # upload only credits "Bamdad".
    if source_artist_tokens:
        title_tokens = _tokens(candidate_title)
        if any(_tok_eq(s, t) for s in source_artist_tokens for t in title_tokens):
            return True
    return False


def score_candidate(
    source_title: str | None,
    source_seconds: int | None,
    candidate_title: str | None,
    candidate_seconds: int | None,
    *,
    source_artist: str | None = None,
    candidate_artists: list[str] | None = None,
) -> float | None:
    """Score a candidate in [0, 1], or None when it is not the same track.

    All three signals must agree: a title match alone lets a different artist
    take the song; a duration match alone lets a different song take the slot;
    an artist match alone says nothing about which song it is.
    """
    title = title_similarity(
        source_title,
        candidate_title,
        source_artist=source_artist,
        candidate_artists=candidate_artists,
    )
    if not artist_evidence(source_artist, source_title, candidate_artists, candidate_title):
        return None

    duration = duration_score(source_seconds, candidate_seconds)
    if duration == 0:
        return None
    if title < 0.5:
        # Uninformative titles exist ("51 minutes with Dariush" vs "DARIUSH
        # CLASSIC MIX"), but an exact-to-the-second length plus the same artist
        # is strong evidence. Anything less still needs the title.
        if duration != 1.0:
            return None
        title = 0.5
    if duration is None and title < 0.8:
        # With no length to verify, demand a strong title match.
        return None
    return 0.5 * title + 0.2 + 0.3 * (duration if duration is not None else 0.5)


def is_plausible_match(
    source_title: str | None,
    source_seconds: int | None,
    candidate_title: str | None,
    candidate_seconds: int | None,
    *,
    source_artist: str | None = None,
    candidate_artists: list[str] | None = None,
) -> bool:
    return (
        score_candidate(
            source_title,
            source_seconds,
            candidate_title,
            candidate_seconds,
            source_artist=source_artist,
            candidate_artists=candidate_artists,
        )
        is not None
    )


def build_queries(artist: str | None, title: str | None) -> list[str]:
    """Search queries for a track, most specific first.

    The uploaded title often already contains "Artist - Song", so searching it
    verbatim is worthwhile; the short song-only form rescues cases where the
    long query buries the right hit (e.g. a Spotify search for a Persian song).
    """
    queries: list[str] = []

    def add(value: str | None) -> None:
        value = (value or "").strip()
        if value and value.casefold() not in {q.casefold() for q in queries}:
            queries.append(value)

    if artist and title:
        add(f"{artist} {title}")
    add(title)
    parts = re.split(r"\s+[-\u2013\u2014]\s+", title or "", maxsplit=1)
    if len(parts) == 2:
        add(parts[1])
        if artist:
            add(f"{artist} {parts[1]}")
    cleaned = re.sub(r"[\(\[].*?[\)\]]", " ", title or "")
    add(re.sub(r"\s+", " ", cleaned).strip())
    return queries


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
