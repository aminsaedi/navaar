#!/usr/bin/env python3
"""One-time Spotify OAuth bootstrap.

Run this locally to complete the browser-based OAuth flow and save
the token cache file. Then copy the cache file to your deployment.

Supports two modes:
  - PKCE (default): No client_secret needed. Uses a public client_id.
  - OAuth: Requires your own client_id + client_secret (needs Premium).

Usage:
    # PKCE mode (no Premium needed):
    python scripts/spotify_auth.py

    # OAuth mode (own app, needs Premium):
    NAVAAR_SPOTIFY_CLIENT_ID=xxx NAVAAR_SPOTIFY_CLIENT_SECRET=yyy python scripts/spotify_auth.py

Environment variables:
    NAVAAR_SPOTIFY_CLIENT_ID      (optional, defaults to public client_id)
    NAVAAR_SPOTIFY_CLIENT_SECRET  (optional, triggers OAuth mode if set)
    NAVAAR_SPOTIFY_REDIRECT_URI   (optional)
    NAVAAR_SPOTIFY_CACHE_PATH     (default: .spotify_cache)
"""
from __future__ import annotations

import os

from spotipy import Spotify
from spotipy.oauth2 import SpotifyOAuth, SpotifyPKCE

SCOPES = "playlist-read-private playlist-read-collaborative playlist-modify-private playlist-modify-public"
DEFAULT_CLIENT_ID = "5c098bcc800e45d49e476265bc9b6934"
DEFAULT_REDIRECT_URI = "http://127.0.0.1:43019/redirect"


def main() -> None:
    client_id = os.environ.get("NAVAAR_SPOTIFY_CLIENT_ID", "")
    client_secret = os.environ.get("NAVAAR_SPOTIFY_CLIENT_SECRET", "")
    redirect_uri = os.environ.get("NAVAAR_SPOTIFY_REDIRECT_URI", "")
    cache_path = os.environ.get("NAVAAR_SPOTIFY_CACHE_PATH", ".spotify_cache")

    if client_secret:
        print("Using OAuth mode (own app with client_secret)")
        auth_manager = SpotifyOAuth(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri or "http://localhost:8888/callback",
            scope=SCOPES,
            cache_path=cache_path,
            open_browser=True,
        )
    else:
        cid = client_id or DEFAULT_CLIENT_ID
        ruri = redirect_uri or DEFAULT_REDIRECT_URI
        print("Using PKCE mode (no client_secret needed)")
        print(f"  Client ID: {cid}")
        print(f"  Redirect:  {ruri}")
        auth_manager = SpotifyPKCE(
            client_id=cid,
            redirect_uri=ruri,
            scope=SCOPES,
            cache_path=cache_path,
            open_browser=True,
        )

    sp = Spotify(auth_manager=auth_manager)
    user = sp.current_user()
    print(f"\nAuthenticated as: {user['display_name']} ({user['id']})")
    print(f"Token cached at:  {cache_path}")
    print("\nCopy this file to your deployment's /data volume.")


if __name__ == "__main__":
    main()
