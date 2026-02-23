from __future__ import annotations

import structlog
from spotipy import Spotify
from spotipy.oauth2 import SpotifyOAuth, SpotifyPKCE
from tenacity import retry, stop_after_attempt, wait_exponential

logger = structlog.get_logger()

SCOPES = "playlist-read-private playlist-read-collaborative playlist-modify-private playlist-modify-public"

# Well-known public client_id (no secret needed, PKCE flow)
DEFAULT_CLIENT_ID = "5c098bcc800e45d49e476265bc9b6934"
DEFAULT_REDIRECT_URI = "http://127.0.0.1:43019/redirect"


class SpotifyClient:
    def __init__(
        self,
        playlist_id: str,
        client_id: str = "",
        client_secret: str = "",
        redirect_uri: str = "",
        cache_path: str = ".spotify_cache",
    ) -> None:
        self._playlist_id = playlist_id

        if client_secret:
            # Full OAuth with own app (Premium account)
            auth_manager = SpotifyOAuth(
                client_id=client_id,
                client_secret=client_secret,
                redirect_uri=redirect_uri or "http://localhost:8888/callback",
                scope=SCOPES,
                cache_path=cache_path,
            )
            logger.info("spotify_auth_mode", mode="oauth")
        else:
            # PKCE flow with public client_id (no secret needed)
            auth_manager = SpotifyPKCE(
                client_id=client_id or DEFAULT_CLIENT_ID,
                redirect_uri=redirect_uri or DEFAULT_REDIRECT_URI,
                scope=SCOPES,
                cache_path=cache_path,
            )
            logger.info("spotify_auth_mode", mode="pkce")

        self._sp = Spotify(auth_manager=auth_manager)
        logger.info("spotify_initialized", playlist_id=playlist_id)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=30))
    def search_track(self, query: str, limit: int = 5) -> list[dict]:
        results = self._sp.search(q=query, type="track", limit=limit)
        tracks = results.get("tracks", {}).get("items", [])
        return [
            {
                "id": t["id"],
                "name": t["name"],
                "artists": [a["name"] for a in t.get("artists", [])],
                "duration_ms": t.get("duration_ms"),
                "uri": t["uri"],
            }
            for t in tracks
        ]

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=30))
    def get_playlist_tracks(self) -> list[dict]:
        tracks: list[dict] = []
        results = self._sp.playlist_items(
            self._playlist_id,
            fields="items(track(id,name,artists(name),duration_ms,uri)),next",
        )
        while results:
            for item in results.get("items", []):
                t = item.get("track")
                if not t or not t.get("id"):
                    continue
                tracks.append({
                    "id": t["id"],
                    "name": t["name"],
                    "artists": [a["name"] for a in t.get("artists", [])],
                    "duration_ms": t.get("duration_ms"),
                    "uri": t.get("uri"),
                })
            if results.get("next"):
                results = self._sp.next(results)
            else:
                break
        logger.debug("sp_playlist_fetched", track_count=len(tracks))
        return tracks

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=30))
    def add_to_playlist(self, track_id: str) -> None:
        self._sp.playlist_add_items(self._playlist_id, [track_id])
        logger.info("sp_added_to_playlist", track_id=track_id)

    def is_in_playlist(
        self, track_id: str, playlist_tracks: list[dict] | None = None
    ) -> bool:
        if playlist_tracks is None:
            playlist_tracks = self.get_playlist_tracks()
        return any(t.get("id") == track_id for t in playlist_tracks)

    def find_best_match(self, artist: str | None, title: str) -> dict | None:
        query = f"{artist} {title}" if artist else title
        results = self.search_track(query)
        if not results:
            return None
        best = results[0]
        logger.info(
            "sp_best_match",
            query=query,
            track_id=best.get("id"),
            match_name=best.get("name"),
        )
        return best
