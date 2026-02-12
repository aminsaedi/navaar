from __future__ import annotations

import json
import time

import httpx
import structlog
from tenacity import retry, stop_after_attempt, wait_exponential

logger = structlog.get_logger()

YT_API_BASE = "https://www.googleapis.com/youtube/v3"


class YTMusicClient:
    """YouTube Music client using the official YouTube Data API v3 with OAuth."""

    def __init__(
        self,
        auth_file: str,
        playlist_id: str,
        client_id: str,
        client_secret: str,
    ) -> None:
        self._auth_file = auth_file
        self._playlist_id = playlist_id
        self._client_id = client_id
        self._client_secret = client_secret
        self._token = self._load_token()
        self._ensure_fresh_token()

    def _load_token(self) -> dict:
        with open(self._auth_file) as f:
            return json.load(f)

    def _save_token(self) -> None:
        with open(self._auth_file, "w") as f:
            json.dump(self._token, f, indent=1)

    def _ensure_fresh_token(self) -> None:
        if self._token.get("expires_at", 0) < time.time() + 60:
            self._refresh_token()

    def _refresh_token(self) -> None:
        resp = httpx.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "refresh_token": self._token["refresh_token"],
                "grant_type": "refresh_token",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        self._token["access_token"] = data["access_token"]
        self._token["expires_at"] = int(time.time()) + data["expires_in"]
        self._token["expires_in"] = data["expires_in"]
        self._save_token()
        logger.debug("oauth_token_refreshed")

    def get_access_token(self) -> str:
        self._ensure_fresh_token()
        return self._token["access_token"]

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.get_access_token()}"}

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=30))
    def search_song(self, query: str, limit: int = 5) -> list[dict]:
        resp = httpx.get(
            f"{YT_API_BASE}/search",
            headers=self._headers(),
            params={
                "part": "snippet",
                "q": query,
                "type": "video",
                "videoCategoryId": "10",  # Music
                "maxResults": limit,
            },
        )
        resp.raise_for_status()
        items = resp.json().get("items", [])
        results = [
            {
                "videoId": item["id"]["videoId"],
                "title": item["snippet"]["title"],
                "artists": [{"name": item["snippet"]["channelTitle"]}],
            }
            for item in items
        ]
        logger.debug("yt_search", query=query, result_count=len(results))
        return results

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=30))
    def get_playlist_tracks(self) -> list[dict]:
        tracks: list[dict] = []
        page_token = None

        while True:
            params: dict[str, str | int] = {
                "part": "snippet",
                "playlistId": self._playlist_id,
                "maxResults": 50,
            }
            if page_token:
                params["pageToken"] = page_token

            resp = httpx.get(
                f"{YT_API_BASE}/playlistItems",
                headers=self._headers(),
                params=params,
            )
            resp.raise_for_status()
            data = resp.json()

            for item in data.get("items", []):
                snippet = item["snippet"]
                tracks.append({
                    "videoId": snippet["resourceId"]["videoId"],
                    "title": snippet["title"],
                    "artists": [{"name": snippet.get("videoOwnerChannelTitle", "")}],
                    "setVideoId": item["id"],  # playlistItem ID, needed for removal
                })

            page_token = data.get("nextPageToken")
            if not page_token:
                break

        logger.debug("yt_playlist_fetched", track_count=len(tracks))
        return tracks

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=30))
    def add_to_playlist(self, video_id: str) -> dict:
        resp = httpx.post(
            f"{YT_API_BASE}/playlistItems",
            headers={**self._headers(), "Content-Type": "application/json"},
            params={"part": "snippet"},
            json={
                "snippet": {
                    "playlistId": self._playlist_id,
                    "resourceId": {
                        "kind": "youtube#video",
                        "videoId": video_id,
                    },
                }
            },
        )
        resp.raise_for_status()
        result = resp.json()
        logger.info("yt_added_to_playlist", video_id=video_id)
        return result

    def is_in_playlist(self, video_id: str, playlist_tracks: list[dict] | None = None) -> bool:
        if playlist_tracks is None:
            playlist_tracks = self.get_playlist_tracks()
        return any(t.get("videoId") == video_id for t in playlist_tracks)

    def find_best_match(self, artist: str | None, title: str) -> dict | None:
        query = f"{artist} {title}" if artist else title
        results = self.search_song(query)
        if not results:
            return None
        best = results[0]
        logger.info(
            "yt_best_match",
            query=query,
            video_id=best.get("videoId"),
            match_title=best.get("title"),
        )
        return best
