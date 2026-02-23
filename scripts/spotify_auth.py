#!/usr/bin/env python3
"""One-time Spotify OAuth bootstrap.

Run this locally to complete the browser-based OAuth flow and save
the token cache file. Then copy the cache file to your deployment.

Usage:
    python scripts/spotify_auth.py

Environment variables (or use a .env file):
    NAVAAR_SPOTIFY_CLIENT_ID
    NAVAAR_SPOTIFY_CLIENT_SECRET
    NAVAAR_SPOTIFY_REDIRECT_URI  (default: http://localhost:8888/callback)
    NAVAAR_SPOTIFY_CACHE_PATH    (default: .spotify_cache)
"""
from __future__ import annotations

import os
import sys

from spotipy import Spotify
from spotipy.oauth2 import SpotifyOAuth


def main() -> None:
    client_id = os.environ.get("NAVAAR_SPOTIFY_CLIENT_ID", "")
    client_secret = os.environ.get("NAVAAR_SPOTIFY_CLIENT_SECRET", "")
    redirect_uri = os.environ.get("NAVAAR_SPOTIFY_REDIRECT_URI", "http://localhost:8888/callback")
    cache_path = os.environ.get("NAVAAR_SPOTIFY_CACHE_PATH", ".spotify_cache")

    if not client_id or not client_secret:
        print("Error: NAVAAR_SPOTIFY_CLIENT_ID and NAVAAR_SPOTIFY_CLIENT_SECRET must be set.")
        sys.exit(1)

    auth_manager = SpotifyOAuth(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        scope="playlist-read-private playlist-modify-private playlist-modify-public",
        cache_path=cache_path,
        open_browser=True,
    )

    sp = Spotify(auth_manager=auth_manager)
    user = sp.current_user()
    print(f"Authenticated as: {user['display_name']} ({user['id']})")
    print(f"Token cached at: {cache_path}")
    print("Copy this file to your deployment's /data volume.")


if __name__ == "__main__":
    main()
