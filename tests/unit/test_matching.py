from __future__ import annotations

import pytest

from navaar.matching import (
    build_queries,
    duration_plausible,
    is_plausible_match,
    normalize_title,
    parse_iso8601_duration,
    score_candidate,
    title_similarity,
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
        # Real pairs from production that are CORRECT. YouTube titles carry the
        # artist and a Persian rendering, so the comparison has to ignore the
        # artist prefix and accept the Persian duplicate.
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
        # Substring containment used to accept these; they are different songs.
        ("Gol", "Golbarg"),
        ("Dele Man Havato Karde 2", "Dele Man Havato Karde"),
        ("After Midnight - 1 Hour Persian Jazz Fusion", "Jazzab"),
    ],
)
def test_titles_overlap_rejects_unrelated(source: str, candidate: str) -> None:
    assert not titles_overlap(source, candidate)


def test_numeric_suffix_is_significant() -> None:
    # "...Karde 2" is a different song from "...Karde"; the trailing 2 matters.
    assert title_similarity("Dele Man Havato Karde 2", "Dele Man Havato Karde") < 0.5
    assert title_similarity("Dele Man Havato Karde 2", "Dele Man Havato Karde 2") >= 0.9


def test_duration_plausible_unknown_passes() -> None:
    assert duration_plausible(None, 300)
    assert duration_plausible(300, None)
    assert duration_plausible(0, 300)


def test_duration_plausible_close_lengths() -> None:
    assert duration_plausible(295, 301)
    assert duration_plausible(183, 190)
    assert duration_plausible(287, 311)  # ~8% edit of the same recording


def test_duration_plausible_rejects_wildly_different() -> None:
    assert not duration_plausible(3089, 300)
    assert not duration_plausible(3826, 218)   # 1-hour mix vs a 3-minute song
    assert not duration_plausible(989, 350)    # long live take vs a short cover
    assert not duration_plausible(1589, 300)   # 26-minute live vs studio


def test_duration_mismatch_rejects_otherwise_exact_title() -> None:
    """A different recording is not the same track even when title+artist match."""
    assert not is_plausible_match(
        "Shabe Meykhooneh", 1589, "Shabe Meykhooneh", 300,
        source_artist="Hayedeh", candidate_artists=["Hayedeh"],
    )
    # Shortened official video vs the album track.
    assert not is_plausible_match(
        "Let It Happen", 471, "Tame Impala - Let It Happen (Official Video)", 257,
        source_artist="Tame Impala", candidate_artists=["tameimpalaVEVO"],
    )


def test_decorated_youtube_title_survives_with_right_length() -> None:
    assert is_plausible_match(
        "Mordab", 295, "Googoosh - Mordab | گوگوش - مرداب", 291,
        source_artist="Googoosh", candidate_artists=["Googoosh"],
    )


def test_same_title_different_artist_is_rejected() -> None:
    # "Leila" by Sijal was mapped to Eljaan's unrelated "Leila" purely on title.
    assert not is_plausible_match(
        "Leila", 181, "Leila", 186,
        source_artist="Sijal", candidate_artists=["Eljaan"],
    )
    assert is_plausible_match(
        "Leila", 181, "Leila", 181,
        source_artist="Sijal", candidate_artists=["Sijal", "Sogand", "Sepehr Khalse"],
    )


def test_same_artist_different_title_is_rejected() -> None:
    # "Gol" was mapped to "Golbarg" because "gol" is a substring of "golbarg".
    assert not is_plausible_match(
        "Gol", 287, "Golbarg", 311,
        source_artist="Hassan Shamaeezadeh", candidate_artists=["Hassan Shamaizadeh"],
    )
    assert is_plausible_match(
        "Gol", 287, "Gol", 286,
        source_artist="Hassan Shamaeezadeh", candidate_artists=["Hassan Shamaizadeh"],
    )


def test_real_artist_in_youtube_title_satisfies_artist_check() -> None:
    # yt→sp: the stored "artist" is the uploader channel; the artist is in the title.
    assert is_plausible_match(
        "Nazanin", None, "Dariush-Ay Nazanin داریوش ـ ای نازنین", None,
        source_artist="Dariush", candidate_artists=["TaranehEnterprise"],
    )
    assert not is_plausible_match(
        "Rage Khab", 289, "Rage Khaab", 160,
        source_artist="Persian Music Group",
        candidate_artists=["Homayoun Shajarian", "Sohrab Pournazeri"],
    )


def test_correct_long_live_match_is_chosen_over_short_cover() -> None:
    source_title = (
        "National Arab Orchestra - Alf Leila wi Leila / الف ليلة وليلى "
        "- Mai Farouk / مي فاروق"
    )
    assert is_plausible_match(
        source_title, 989, "Alf Leila We Leila Live", 986,
        source_artist="National Arab Orchestra", candidate_artists=["Mai Farouk"],
    )
    assert not is_plausible_match(
        source_title, 989, "Alf Leila We Leila", 350,
        source_artist="National Arab Orchestra",
        candidate_artists=["The Arab Orchestra Of Nazareth", "Lubna Salame"],
    )


def test_no_source_duration_requires_a_strong_title() -> None:
    # yt→sp used to accept *anything* without a duration. That is the bug.
    assert not is_plausible_match("Anything", None, "Totally Unrelated Song", 300)
    assert is_plausible_match(
        "Nazanin", None, "Dariush - Nazanin", 317,
        source_artist="Dariush", candidate_artists=["Dariush"],
    )


def test_score_candidate_prefers_exact_recording() -> None:
    exact = score_candidate(
        "Gol", 287, "Gol", 286,
        source_artist="Hassan Shamaeezadeh", candidate_artists=["Hassan Shamaizadeh"],
    )
    wrong = score_candidate(
        "Gol", 287, "Golbarg", 311,
        source_artist="Hassan Shamaeezadeh", candidate_artists=["Hassan Shamaizadeh"],
    )
    assert exact is not None and exact > 0
    assert wrong is None


def test_build_queries_adds_song_only_and_cleaned_forms() -> None:
    queries = build_queries(
        "Farsi Jazz", "After Midnight (Official Video) - Persian Jazz"
    )
    assert queries[0] == "Farsi Jazz After Midnight (Official Video) - Persian Jazz"
    assert "After Midnight (Official Video) - Persian Jazz" in queries
    assert any("Persian Jazz" in q and q != queries[0] for q in queries)
    assert any("Official Video" not in q for q in queries)


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


@pytest.mark.parametrize(
    "source,candidate",
    [
        # Persian romanisation drift seen in production: these are the same song.
        ("Ghame Tanhaei", "Gham-E Tanhaee"),
        ("Be Bachehamoon Chi Begim", "Beh Bachehamoon Chee Begeem"),
        ("Faje-eh", "Fajaeh"),
        ("Maman Bozorg [MusText.Net]", "KELK: MAMAN BOZORG"),
        ("Ay Eshgh", "Ey Eshgh"),
    ],
)
def test_transliteration_variants_match(source: str, candidate: str) -> None:
    assert title_similarity(source, candidate) >= 0.5


def test_shared_first_name_is_not_artist_evidence() -> None:
    # "Mohsen Yeganeh" must not match "Mohsen Chavoshi" on "mohsen" alone.
    assert not is_plausible_match(
        "Mohsen Yeganeh - Rage Khab", 289, "Khab", 325,
        source_artist="Persian Music Group",
        candidate_artists=["Mohsen Chavoshi"],
    )


def test_multi_artist_source_matches_on_one_name() -> None:
    # "Bamdad & Tahrang" vs an upload that only credits Bamdad.
    assert is_plausible_match(
        "Fasle Sarma", 269, "Bamdad - Fasle Sarma (Lyric Video)", 270,
        source_artist="Bamdad & Tahrang", candidate_artists=["Pelak Records"],
    )
    assert is_plausible_match(
        "Ageh Eshgh Hamineh", 335, "Ageh Eshgh Hamineh", 337,
        source_artist="Andy", candidate_artists=["Andy & Kouros"],
    )


def test_exact_duration_rescues_uninformative_title() -> None:
    # The 51-minute Telegram programme maps to a 51-minute Dariush mix whose
    # title describes the format, not the song.
    assert is_plausible_match(
        "۵۱دقیقه همراه با داریوش", 3089, "DARIUSH CLASSIC MIX 🌙 | آهنگهای به یاد ماندنی داریوش", 3090,
        source_artist="داریوش", candidate_artists=["Caltex Records"],
    )
    # But an exact duration alone cannot rescue a different artist.
    assert not is_plausible_match(
        "۵۱دقیقه همراه با داریوش", 3089, "DARIUSH CLASSIC MIX", 3090,
        source_artist="Someone Else", candidate_artists=["Caltex Records"],
    )
