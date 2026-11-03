from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from navaar.ytmusic.client import YTMusicClient


def _write_token(path: Path, *, expires_at: float) -> None:
    path.write_text(
        json.dumps(
            {
                "access_token": "at",
                "refresh_token": "rt",
                "expires_at": expires_at,
                "expires_in": 3600,
            }
        )
    )


@pytest.fixture
def fresh_client(tmp_path: Path) -> YTMusicClient:
    auth = tmp_path / "oauth.json"
    _write_token(auth, expires_at=time.time() + 100000)  # far future -> no refresh
    return YTMusicClient(
        auth_file=str(auth), playlist_id="PL", client_id="cid", client_secret="sec"
    )


def test_no_refresh_when_token_fresh(fresh_client: YTMusicClient) -> None:
    # Should not raise / attempt a network refresh for a token valid for hours.
    assert fresh_client.get_access_token() == "at"


def test_save_token_is_atomic_and_valid(fresh_client: YTMusicClient, tmp_path: Path) -> None:
    fresh_client._token["access_token"] = "new-at"
    fresh_client._save_token()
    auth = tmp_path / "oauth.json"
    # File parses cleanly (no half-written JSON) and no temp file is left behind.
    reloaded = json.loads(auth.read_text())
    assert reloaded["access_token"] == "new-at"
    assert not (tmp_path / "oauth.json.tmp").exists()


def test_ensure_fresh_double_checked(monkeypatch, fresh_client: YTMusicClient) -> None:
    # Token already fresh: _refresh_token must never be called (double-checked guard).
    calls = {"n": 0}

    def _boom() -> None:
        calls["n"] += 1

    monkeypatch.setattr(fresh_client, "_refresh_token", _boom)
    fresh_client._ensure_fresh_token()
    assert calls["n"] == 0


class _Resp:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


def _search_payload(*items: tuple[str, str]) -> dict:
    return {
        "items": [
            {"id": {"videoId": vid}, "snippet": {"title": title, "channelTitle": "ch"}}
            for vid, title in items
        ]
    }


def _videos_payload(*items: tuple[str, str]) -> dict:
    return {"items": [{"id": vid, "contentDetails": {"duration": iso}} for vid, iso in items]}


def _stub_httpx(monkeypatch, search: dict, videos: dict, calls: list[str] | None = None):
    def fake_get(url: str, **kwargs):
        if calls is not None:
            calls.append(url)
        return _Resp(videos if url.endswith("/videos") else search)

    monkeypatch.setattr("navaar.ytmusic.client.httpx.get", fake_get)


def test_find_best_match_rejects_unrelated_long_programme(
    fresh_client: YTMusicClient, monkeypatch
) -> None:
    """The regression: a 51-minute programme matched an unrelated 5-minute song."""
    _stub_httpx(
        monkeypatch,
        _search_payload(("IpU_1h946m8", "Dariush: Vahm | Official Lyric Video")),
        _videos_payload(("IpU_1h946m8", "PT5M0S")),
    )
    assert fresh_client.find_best_match("Dariush", "۵۱دقیقه همراه با داریوش", 3089) is None


def test_find_best_match_accepts_decorated_title_with_right_length(
    fresh_client: YTMusicClient, monkeypatch
) -> None:
    _stub_httpx(
        monkeypatch,
        _search_payload(("vid1", "Googoosh - Mordab | گوگوش - مرداب")),
        _videos_payload(("vid1", "PT4M51S")),
    )
    match = fresh_client.find_best_match("Googoosh", "Mordab", 295)
    assert match is not None and match["videoId"] == "vid1"


def test_find_best_match_skips_bad_candidate_for_good_one(
    fresh_client: YTMusicClient, monkeypatch
) -> None:
    _stub_httpx(
        monkeypatch,
        _search_payload(("bad", "Unrelated Interview"), ("good", "Sattar - Akharin Talash")),
        _videos_payload(("bad", "PT58M0S"), ("good", "PT4M25S")),
    )
    match = fresh_client.find_best_match("Sattar", "Akharin Talash", 265)
    assert match is not None and match["videoId"] == "good"


def test_find_best_match_prefers_album_length_over_short_music_video(
    fresh_client: YTMusicClient, monkeypatch
) -> None:
    """The official video (4:17) is a different edit of the 7:47 album track."""
    _stub_httpx(
        monkeypatch,
        _search_payload(
            ("video", "Tame Impala - Let It Happen (Official Video)"),
            ("album", "Tame Impala - Let It Happen (Official Audio)"),
        ),
        _videos_payload(("video", "PT4M17S"), ("album", "PT7M55S")),
    )
    match = fresh_client.find_best_match("Tame Impala", "Let It Happen", 471)
    assert match is not None and match["videoId"] == "album"


def test_find_best_match_rejects_same_title_live_take(
    fresh_client: YTMusicClient, monkeypatch
) -> None:
    """A live duet of the same song is not the 6-minute studio recording."""
    _stub_httpx(
        monkeypatch,
        _search_payload(("live", "Dariush: Ay Eshgh (Featuring Faramarz Aslani)")),
        _videos_payload(("live", "PT4M3S")),
    )
    assert fresh_client.find_best_match("Dariush", "Ay Eshgh", 369) is None


def test_find_best_match_without_source_duration_verifies_title_and_artist(
    fresh_client: YTMusicClient, monkeypatch
) -> None:
    """The voice flow passes no duration; identity still has to be proven."""
    _stub_httpx(
        monkeypatch,
        _search_payload(
            ("wrong", "Somebody Else - Totally Unrelated"),
            ("right", "Dariush - Nazanin"),
        ),
        _videos_payload(("wrong", "PT3M0S"), ("right", "PT5M17S")),
    )
    match = fresh_client.find_best_match("Dariush", "Nazanin")
    assert match is not None and match["videoId"] == "right"


def test_duration_lookup_failure_does_not_break_search(
    fresh_client: YTMusicClient, monkeypatch
) -> None:
    """Losing the duration signal must fall back to title+artist, not fail."""
    def fake_get(url: str, **kwargs):
        if url.endswith("/videos"):
            raise RuntimeError("quota exceeded")
        return _Resp(_search_payload(("vid1", "Sattar - Akharin Talash")))

    monkeypatch.setattr("navaar.ytmusic.client.httpx.get", fake_get)
    match = fresh_client.find_best_match("Sattar", "Akharin Talash", 265)
    assert match is not None and match["videoId"] == "vid1"
