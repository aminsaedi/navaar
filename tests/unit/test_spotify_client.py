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
