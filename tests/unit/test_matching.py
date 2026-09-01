from __future__ import annotations

import pytest

from navaar.matching import (
    duration_plausible,
    is_plausible_match,
    normalize_title,
    parse_iso8601_duration,
    titles_overlap,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Faramarz Aslani - &quot;Yar&quot; OFFICIAL VIDEO", "faramarz aslani yar official video"),
        ("Div O Fereshteh", "div o fereshteh"),
        ("500 Miles (2004 Remaster)", "500 miles 2004 remaster"),
        (None, ""),
        ("", ""),
    ],
)
def test_normalize_title(raw: str | None, expected: str) -> None:
    assert normalize_title(raw) == expected


@pytest.mark.parametrize(
    "source,candidate",
    [
        # Real pairs from the production sync log that are CORRECT matches. The
        # YouTube titles carry the artist and a Persian rendering, which is why
        # this is containment and not similarity scoring.
        ("Parvardegar", "Moein - Parvardegara معین پروردگارا"),
        ("Yar", 'Faramarz Aslani - &quot;Yar&quot; OFFICIAL VIDEO'),
        ("Akharin Talash", "Sattar -  Akharin Talash ستار- آخرین تلاش"),
        ("Div O Fereshteh", "Mohsen Namjoo - Div o Fereshteh محسن نامجو - دیو و فرشته"),
        ("Toloo", "Shohreh-Toloo | شهره ـ طلوع"),
        ("Shabe Meykhooneh", "Shabe Meykhooneh"),
    ],
)
def test_titles_overlap_accepts_real_correct_matches(source: str, candidate: str) -> None:
    assert titles_overlap(source, candidate)


@pytest.mark.parametrize(
    "source,candidate",
    [
        ("۵۱دقیقه همراه با داریوش", "Dariush: Vahm | داریوش: توهم توطئه  | Official Lyric Video"),
        ("۵۱دقیقه همراه با داریوش", "Faryad Zire Ab - Live"),
        ("Tamanna", "Benshin Tamashayat Konam"),
    ],
)
def test_titles_overlap_rejects_unrelated(source: str, candidate: str) -> None:
    assert not titles_overlap(source, candidate)


def test_duration_plausible_unknown_passes() -> None:
    # Absence of evidence is not evidence of a bad match.
    assert duration_plausible(None, 300)
    assert duration_plausible(300, None)
    assert duration_plausible(0, 300)


def test_duration_plausible_close_lengths() -> None:
    assert duration_plausible(295, 301)
    assert duration_plausible(183, 190)


def test_duration_plausible_rejects_wildly_different() -> None:
    # 51-minute programme vs a 5-minute song.
    assert not duration_plausible(3089, 300)
    assert not duration_plausible(300, 3089)


def test_long_live_version_survives_on_title() -> None:
    """The 26-minute live "Shabe Meykhooneh" against a short studio cut: the
    duration signal fails, the title signal carries it, so it must be accepted."""
    assert not duration_plausible(1589, 300)
    assert is_plausible_match("Shabe Meykhooneh", 1589, "Shabe Meykhooneh", 300)


def test_decorated_youtube_title_survives_on_duration() -> None:
    """Inverse case: title is heavily decorated but the length matches."""
    assert is_plausible_match("Mordab", 295, "Googoosh - Mordab | گوگوش - مرداب", 291)


def test_rejects_when_both_signals_fail() -> None:
    """The regression this exists for: a 51-minute programme matched to an
    unrelated short song and silently added to both playlists."""
    assert not is_plausible_match(
        "۵۱دقیقه همراه با داریوش", 3089, "Faryad Zire Ab - Live", 356
    )
    assert not is_plausible_match(
        "۵۱دقیقه همراه با داریوش", 3089, "Dariush: Vahm | Official Lyric Video", 300
    )


def test_no_source_duration_falls_back_to_previous_behaviour() -> None:
    """yt→sp has no stored duration; nothing should start being rejected there."""
    assert is_plausible_match("Anything", None, "Totally Unrelated Song", 300)


@pytest.mark.parametrize(
    "iso,expected",
    [
        ("PT4M13S", 253),
        ("PT1H2M3S", 3723),
        ("PT51M29S", 3089),
        ("PT45S", 45),
        ("P0D", None),
        ("", None),
        (None, None),
        ("garbage", None),
    ],
)
def test_parse_iso8601_duration(iso: str | None, expected: int | None) -> None:
    assert parse_iso8601_duration(iso) == expected
