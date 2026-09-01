from __future__ import annotations

import json
import os
import threading
import time

import httpx
import structlog
from tenacity import retry, stop_after_attempt, wait_exponential

from navaar.auth_errors import retry_if_transient
from navaar.matching import is_plausible_match, parse_iso8601_duration
from navaar.metrics import AUTH_ERRORS

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
        # A single client is shared across up to five directions, and every method
        # runs on an OS thread via asyncio.to_thread — so the token refresh + on-disk
        # save must be serialized. Without this, two threads that both observe the
        # token stale can both truncate-and-rewrite the auth file concurrently and
        # leave it as invalid JSON, which crashes the *next* boot (the file is the
        # only copy of the refresh token).
        self._token_lock = threading.Lock()
        self._token = self._load_token()
        self._ensure_fresh_token()

    def _load_token(self) -> dict:
        with open(self._auth_file) as f:
            return json.load(f)

    def _save_token(self) -> None:
        # Atomic write: dump to a temp file in the same dir, then os.replace so a
        # crash or a racing writer can never leave a half-written token file.
        tmp = f"{self._auth_file}.tmp"
        with open(tmp, "w") as f:
            json.dump(self._token, f, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self._auth_file)

    def _ensure_fresh_token(self) -> None:
        if self._token.get("expires_at", 0) >= time.time() + 60:
            return
        with self._token_lock:
            # Re-check under the lock: another thread may have refreshed while we
            # waited, so we don't refresh (and rewrite the file) twice.
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
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            # A revoked/expired Google refresh token never recovers without
            # operator re-auth — surface it distinctly rather than as a generic crash.
            if e.response.status_code in (400, 401, 403):
                AUTH_ERRORS.labels(service="yt").inc()
                logger.error(
                    "oauth_refresh_failed",
                    service="yt",
                    status=e.response.status_code,
                    body=resp.text[:200],
                )
            raise
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

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=30), retry=retry_if_transient)
    def search_song(self, query: str, limit: int = 5, with_duration: bool = False) -> list[dict]:
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
        if with_duration and results:
            durations = self._fetch_durations([r["videoId"] for r in results])
            for r in results:
                r["duration_seconds"] = durations.get(r["videoId"])
        logger.debug("yt_search", query=query, result_count=len(results))
        return results

    def _fetch_durations(self, video_ids: list[str]) -> dict[str, int]:
        """Durations for search hits, which `search` itself never returns.

        Best-effort: a failure here must not fail the search, it only costs the
        duration signal. One videos.list call is 1 quota unit against the 100 the
        search already spent, so this is noise in the budget.
        """
        try:
            resp = httpx.get(
                f"{YT_API_BASE}/videos",
                headers=self._headers(),
                params={"part": "contentDetails", "id": ",".join(video_ids)},
            )
            resp.raise_for_status()
            out: dict[str, int] = {}
            for item in resp.json().get("items", []):
                seconds = parse_iso8601_duration(item.get("contentDetails", {}).get("duration"))
                if seconds:
                    out[item["id"]] = seconds
            return out
        except Exception:
            logger.warning("yt_duration_lookup_failed", count=len(video_ids), exc_info=True)
            return {}

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=30), retry=retry_if_transient)
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

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=30), retry=retry_if_transient)
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

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=30), retry=retry_if_transient)
    def remove_from_playlist(self, video_id: str, playlist_tracks: list[dict] | None = None) -> bool:
        """Remove the playlist entry for ``video_id``. The Data API deletes by the
        playlistItem id (``setVideoId``), so look it up from the playlist first.
        Returns False when the track isn't in the playlist."""
        if playlist_tracks is None:
            playlist_tracks = self.get_playlist_tracks()
        set_video_id = next(
            (t.get("setVideoId") for t in playlist_tracks if t.get("videoId") == video_id),
            None,
        )
        if not set_video_id:
            logger.info("yt_remove_not_in_playlist", video_id=video_id)
            return False
        resp = httpx.delete(
            f"{YT_API_BASE}/playlistItems",
            headers=self._headers(),
            params={"id": set_video_id},
        )
        resp.raise_for_status()
        logger.info("yt_removed_from_playlist", video_id=video_id, set_video_id=set_video_id)
        return True

    def is_in_playlist(self, video_id: str, playlist_tracks: list[dict] | None = None) -> bool:
        if playlist_tracks is None:
            playlist_tracks = self.get_playlist_tracks()
        return any(t.get("videoId") == video_id for t in playlist_tracks)

    def find_best_match(
        self, artist: str | None, title: str, duration_seconds: int | None = None
    ) -> dict | None:
        query = f"{artist} {title}" if artist else title
        # Only pay for the duration lookup when there is a source duration to
        # compare it against (yt→sp has none).
        results = self.search_song(query, with_duration=duration_seconds is not None)
        if not results:
            return None

        for candidate in results:
            if is_plausible_match(
                title, duration_seconds, candidate.get("title"), candidate.get("duration_seconds")
            ):
                logger.info(
                    "yt_best_match",
                    query=query,
                    video_id=candidate.get("videoId"),
                    match_title=candidate.get("title"),
                )
                return candidate

        # Every hit was a different work — search always returns *something*, so
        # this is the "not on YouTube" case, not a reason to take the top hit.
        logger.info(
            "yt_no_plausible_match",
            query=query,
            duration_seconds=duration_seconds,
            rejected=[(r.get("title"), r.get("duration_seconds")) for r in results],
        )
        return None
