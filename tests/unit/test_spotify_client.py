from __future__ import annotations

from unittest.mock import MagicMock, patch

from spotipy.oauth2 import SpotifyOAuth, SpotifyPKCE

from navaar.spotify.client import DEFAULT_CLIENT_ID, SpotifyClient

# These construct the REAL SpotifyClient (not a MagicMock) so the auth-manager
# wiring is exercised against the installed spotipy. This is the surface that the
# `get_access_token(as_dict=...)` version-drift bug lived on — a construction
# smoke test turns that class of breakage into a CI failure instead of a runtime
# auth outage. `Spotify` is patched so nothing touches the network.


@patch("navaar.spotify.client.Spotify")
def test_pkce_mode_builds_pkce_auth_manager(mock_spotify: MagicMock, tmp_path) -> None:
    cache = tmp_path / ".spotify_cache"
    client = SpotifyClient(
        playlist_id="pl123",
        cache_path=str(cache),
    )
    assert client is not None
    # No client_secret -> PKCE flow with the public client_id.
    mock_spotify.assert_called_once()
    auth_manager = mock_spotify.call_args.kwargs["auth_manager"]
    assert isinstance(auth_manager, SpotifyPKCE)
    assert auth_manager.client_id == DEFAULT_CLIENT_ID


@patch("navaar.spotify.client.Spotify")
def test_oauth_mode_builds_oauth_auth_manager(mock_spotify: MagicMock, tmp_path) -> None:
    cache = tmp_path / ".spotify_cache"
    client = SpotifyClient(
        playlist_id="pl123",
        client_id="my_id",
        client_secret="my_secret",
        redirect_uri="http://localhost:8888/callback",
        cache_path=str(cache),
    )
    assert client is not None
    auth_manager = mock_spotify.call_args.kwargs["auth_manager"]
    assert isinstance(auth_manager, SpotifyOAuth)
    assert auth_manager.client_id == "my_id"


def _sp_client(mock_spotify: MagicMock, tmp_path, search_items: list[dict]) -> SpotifyClient:
    mock_spotify.return_value.search.return_value = {"tracks": {"items": search_items}}
    return SpotifyClient(playlist_id="pl123", cache_path=str(tmp_path / ".spotify_cache"))


def _item(track_id: str, name: str, duration_ms: int) -> dict:
    return {"id": track_id, "name": name, "artists": [{"name": "Dariush"}],
            "duration_ms": duration_ms, "uri": f"spotify:track:{track_id}"}


@patch("navaar.spotify.client.Spotify")
def test_find_best_match_rejects_wrong_length_and_wrong_title(
    mock_spotify: MagicMock, tmp_path
) -> None:
    """A 51-minute programme is not on Spotify; search still returns a confident
    top hit. Reporting no match beats silently adding an unrelated song."""
    client = _sp_client(
        mock_spotify, tmp_path, [_item("6kU3", "Faryad Zire Ab - Live", 356_336)]
    )
    assert client.find_best_match("Dariush", "۵۱دقیقه همراه با داریوش", 3089) is None


@patch("navaar.spotify.client.Spotify")
def test_find_best_match_keeps_long_live_version_with_matching_title(
    mock_spotify: MagicMock, tmp_path
) -> None:
    client = _sp_client(mock_spotify, tmp_path, [_item("4QQ", "Shabe Meykhooneh", 400_000)])
    match = client.find_best_match("Hayedeh", "Shabe Meykhooneh", 1589)
    assert match is not None and match["id"] == "4QQ"


@patch("navaar.spotify.client.Spotify")
def test_find_best_match_skips_to_the_plausible_candidate(
    mock_spotify: MagicMock, tmp_path
) -> None:
    client = _sp_client(
        mock_spotify,
        tmp_path,
        [_item("bad", "Something Else Entirely", 20_000), _item("good", "Mordab", 291_000)],
    )
    match = client.find_best_match("Googoosh", "Mordab", 295)
    assert match is not None and match["id"] == "good"


@patch("navaar.spotify.client.Spotify")
def test_find_best_match_without_source_duration_takes_top_hit(
    mock_spotify: MagicMock, tmp_path
) -> None:
    """yt→sp stores no duration — behaviour there must be unchanged."""
    client = _sp_client(mock_spotify, tmp_path, [_item("top", "Whatever", 200_000)])
    match = client.find_best_match("A", "Unrelated Title")
    assert match is not None and match["id"] == "top"
