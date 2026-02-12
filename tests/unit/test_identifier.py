from __future__ import annotations

import pytest

from navaar.sync.identifier import (
    identify_from_filename,
    identify_from_tg_metadata,
    identify_track,
)


class TestIdentifyFromFilename:
    def test_artist_dash_title(self) -> None:
        result = identify_from_filename("Adele - Hello.mp3")
        assert result is not None
        assert result.artist == "Adele"
        assert result.title == "Hello"
        assert result.method == "filename"

    def test_artist_emdash_title(self) -> None:
        result = identify_from_filename("The Weeknd — Blinding Lights.mp3")
        assert result is not None
        assert result.artist == "The Weeknd"
        assert result.title == "Blinding Lights"

    def test_no_separator(self) -> None:
        result = identify_from_filename("some_random_track.mp3")
        assert result is not None
        assert result.artist is None
        assert result.title == "some_random_track"

    def test_strips_official_video(self) -> None:
        result = identify_from_filename("Artist - Song (Official Video).mp3")
        assert result is not None
        assert result.title == "Song"

    def test_strips_brackets(self) -> None:
        result = identify_from_filename("Artist - Song [HD].flac")
        assert result is not None
        assert result.title == "Song"

    def test_none_filename(self) -> None:
        assert identify_from_filename(None) is None

    def test_empty_filename(self) -> None:
        assert identify_from_filename("") is None


class TestIdentifyFromTgMetadata:
    def test_with_title_and_performer(self) -> None:
        result = identify_from_tg_metadata("Adele", "Hello")
        assert result is not None
        assert result.artist == "Adele"
        assert result.title == "Hello"
        assert result.method == "tg_metadata"

    def test_title_only(self) -> None:
        result = identify_from_tg_metadata(None, "Hello")
        assert result is not None
        assert result.artist is None
        assert result.title == "Hello"

    def test_no_title(self) -> None:
        result = identify_from_tg_metadata("Adele", None)
        assert result is None


class TestIdentifyTrack:
    def test_pipeline_tg_metadata_first(self) -> None:
        result = identify_track(
            tg_performer="Adele",
            tg_title="Hello",
            file_name="random_file.mp3",
        )
        assert result is not None
        assert result.method == "tg_metadata"
        assert result.title == "Hello"

    def test_pipeline_falls_back_to_filename(self) -> None:
        result = identify_track(
            tg_performer=None,
            tg_title=None,
            file_name="Queen - Bohemian Rhapsody.mp3",
        )
        assert result is not None
        assert result.method == "filename"
        assert result.artist == "Queen"
        assert result.title == "Bohemian Rhapsody"

    def test_pipeline_all_none(self) -> None:
        result = identify_track()
        assert result is None
