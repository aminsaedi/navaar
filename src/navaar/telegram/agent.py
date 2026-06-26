from __future__ import annotations

import asyncio
import json
import re
from typing import TYPE_CHECKING

import httpx
import structlog

from navaar.telegram.cards import _STATUS_ICON, _SVC_ICON, _SVC_LABEL

if TYPE_CHECKING:
    from telegram import Bot

    from navaar.db.models import Track
    from navaar.db.repository import TrackRepository
    from navaar.sync.engine import SyncEngine
    from navaar.telegram.cards import TrackCardService

logger = structlog.get_logger()

_ACTIONS = {"status", "unsync", "resync", "retry", "delete", "none"}
_PLATFORMS = {"yt", "sp", "all", None}

_SYSTEM_PROMPT = (
    "You are the intent parser for Navaar, a bot that mirrors music tracks across a "
    "Telegram channel, a YouTube Music playlist, and a Spotify playlist.\n"
    "Given the track in context and the user's request, decide what to do and reply.\n\n"
    "Respond with ONLY a single JSON object (no prose, no markdown fences):\n"
    '{"action": one of ["status","unsync","resync","retry","delete","none"], '
    '"platform": one of ["yt","sp","all",null], '
    '"track_id": integer or null, '
    '"reply": a short friendly message to the user}\n\n'
    "Guidance:\n"
    "- status: the user asks where/how the track synced, links, or why it failed.\n"
    "- unsync: remove the track from a platform's playlist. platform 'yt', 'sp', or "
    "'all'. Default to 'all' if unspecified.\n"
    "- resync/retry: re-add / re-attempt syncing the track to a platform (default 'all').\n"
    "- delete: remove the track from all playlists and forget it entirely.\n"
    "- none: anything else (questions, chit-chat) — put your full answer in 'reply'.\n"
    "- track_id: only set it if the user names a specific track id like '#42'; "
    "otherwise null (the track in context is used).\n"
    "Keep 'reply' to one short sentence."
)


def _origin(prefix_or_track: str | Track) -> str:
    direction = prefix_or_track if isinstance(prefix_or_track, str) else prefix_or_track.direction
    return direction.split("_to_")[0]


class NavaarAgent:
    """Natural-language control surface. The configured OpenAI-compatible endpoint
    parses a request into a constrained action; this class executes it against the
    repository, the playlist clients, and the sync engine. The model never executes
    anything — it only selects action/platform/track_id.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout: int,
        bot: Bot,
        channel_id: int,
        track_repo: TrackRepository,
        engine: SyncEngine | None,
        card_service: TrackCardService | None,
        yt_client: object,
        sp_client: object | None,
        sp_enabled: bool,
        enabled: bool = True,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout = timeout
        self._bot = bot
        self._channel_id = channel_id
        self._tracks = track_repo
        self._engine = engine
        self._card = card_service
        self._yt = yt_client
        self._sp = sp_client
        self._sp_enabled = sp_enabled
        self.enabled = enabled and bool(base_url)

    # ── LLM intent parsing ───────────────────────────────────────────

    async def parse_intent(self, track_ctx: str, user_text: str) -> dict:
        """Ask the endpoint to classify the request. Always returns a dict with a
        valid ``action``; never raises (falls back to action='none')."""
        payload = {
            "model": self._model,
            "max_tokens": 400,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": f"TRACK IN CONTEXT:\n{track_ctx}\n\nUSER: {user_text}"},
            ],
        }
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    f"{self._base_url}/chat/completions", json=payload, headers=headers
                )
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"]["content"]
        except Exception:
            logger.warning("nl_agent_request_failed", exc_info=True)
            return {"action": "none", "platform": None, "track_id": None,
                    "reply": "Sorry, I couldn't reach my language model just now."}

        intent = self._parse_json(content)
        if intent is None:
            return {"action": "none", "platform": None, "track_id": None,
                    "reply": content.strip()[:500] or "I didn't understand that."}

        action = intent.get("action")
        if action not in _ACTIONS:
            action = "none"
        platform = intent.get("platform")
        if platform not in _PLATFORMS:
            platform = None
        track_id = intent.get("track_id")
        track_id = track_id if isinstance(track_id, int) else None
        return {
            "action": action,
            "platform": platform,
            "track_id": track_id,
            "reply": str(intent.get("reply") or ""),
        }

    @staticmethod
    def _parse_json(content: str) -> dict | None:
        text = content.strip()
        # Strip ```json … ``` fences if present.
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
            text = re.sub(r"\n?```$", "", text).strip()
        try:
            obj = json.loads(text)
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            pass
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                obj = json.loads(match.group(0))
                return obj if isinstance(obj, dict) else None
            except json.JSONDecodeError:
                return None
        return None

    # ── Orchestration ────────────────────────────────────────────────

    async def run(self, *, message_text: str, siblings: list[Track] | None = None) -> str:
        """Parse the request and execute it. ``siblings`` is the logical track the
        user explicitly replied to (channel); when None (DM) the target comes from
        an id in the request or falls back to the most recent track."""
        ctx = self._context_text(siblings) if siblings else "(no specific track — DM)"
        intent = await self.parse_intent(ctx, message_text)
        action = intent["action"]

        if action == "none":
            return intent["reply"] or "I'm not sure what you'd like me to do."

        target = await self._resolve_target(siblings, intent["track_id"])
        if not target:
            return "I couldn't tell which track you mean. Reply to its message, or give an id like #42."

        logger.info(
            "nl_agent_action",
            action=action, platform=intent["platform"], track_id=target[0].id,
        )
        if action == "status":
            return self._context_text(target)
        if action == "unsync":
            return await self._unsync(target, intent["platform"] or "all")
        if action in ("resync", "retry"):
            return await self._resync(target, intent["platform"] or "all")
        if action == "delete":
            return await self._delete(target)
        return intent["reply"] or "Done."

    async def _resolve_target(
        self, siblings: list[Track] | None, track_id: int | None
    ) -> list[Track] | None:
        if siblings:
            return siblings  # explicit reply target wins
        if track_id is not None:
            track = await self._tracks.get_track(track_id)
            return await self._tracks.get_sibling_tracks(track) if track else None
        recent = await self._tracks.get_recent_tracks(limit=1)
        return await self._tracks.get_sibling_tracks(recent[0]) if recent else None

    # ── Actions ──────────────────────────────────────────────────────

    @staticmethod
    def _ext_ids(siblings: list[Track]) -> tuple[str | None, str | None]:
        yt = next((s.yt_video_id for s in siblings if s.yt_video_id), None)
        sp = next((s.sp_track_id for s in siblings if s.sp_track_id), None)
        return yt, sp

    @staticmethod
    def _row_for(siblings: list[Track], svc: str) -> Track | None:
        return next((s for s in siblings if s.direction.endswith(f"_to_{svc}")), None)

    async def _unsync(self, siblings: list[Track], platform: str) -> str:
        yt_id, sp_id = self._ext_ids(siblings)
        results: list[str] = []

        if platform in ("yt", "all"):
            if yt_id:
                removed = await asyncio.to_thread(self._yt.remove_from_playlist, yt_id)
                row = self._row_for(siblings, "yt")
                if row:
                    await self._tracks.update_track(row.id, status="unsynced")
                results.append("✅ removed from YouTube Music" if removed
                               else "ℹ️ wasn't in the YouTube Music playlist")
            else:
                results.append("ℹ️ no YouTube Music entry to remove")

        if platform in ("sp", "all") and self._sp_enabled:
            if sp_id and self._sp is not None:
                await asyncio.to_thread(self._sp.remove_from_playlist, sp_id)
                row = self._row_for(siblings, "sp")
                if row:
                    await self._tracks.update_track(row.id, status="unsynced")
                results.append("✅ removed from Spotify")
            else:
                results.append("ℹ️ no Spotify entry to remove")

        await self._refresh_card(siblings)
        return self._headline(siblings, "Unsynced") + "\n" + "\n".join(results)

    async def _resync(self, siblings: list[Track], platform: str) -> str:
        results: list[str] = []
        targets = [s for s in ("yt", "sp") if platform in (s, "all")]
        for svc in targets:
            if svc == "sp" and not self._sp_enabled:
                continue
            row = self._row_for(siblings, svc)
            if not row:
                results.append(f"ℹ️ no {_SVC_LABEL[svc]} sync to redo (it's the source)")
                continue
            await self._tracks.reset_for_retry(row.id)
            if self._engine is not None:
                self._engine.force_sync(row.direction)
            results.append(f"🔄 re-queued for {_SVC_LABEL[svc]}")

        await self._refresh_card(siblings)
        return self._headline(siblings, "Resync") + "\n" + "\n".join(
            results or ["nothing to resync"]
        )

    async def _delete(self, siblings: list[Track]) -> str:
        await self._unsync(siblings, "all")
        card_id = next((s.card_message_id for s in siblings if s.card_message_id), None)
        if card_id:
            try:
                await self._bot.delete_message(chat_id=self._channel_id, message_id=card_id)
            except Exception:
                logger.warning("nl_agent_card_delete_failed", exc_info=True)
        ids = [s.id for s in siblings]
        for tid in ids:
            await self._tracks.delete_track(tid)
        return f"🗑 Deleted track #{ids[0]} and removed it from all playlists."

    async def _refresh_card(self, siblings: list[Track]) -> None:
        if self._card is not None:
            await self._card.refresh(siblings[0].id)

    # ── Presentation ─────────────────────────────────────────────────

    @staticmethod
    def _headline(siblings: list[Track], verb: str) -> str:
        primary = siblings[0]
        return f"{verb}: {primary.artist or 'Unknown'} — {primary.title} (#{primary.id})"

    def _context_text(self, siblings: list[Track]) -> str:
        primary = siblings[0]
        prefix = _origin(primary)
        by_dir = {s.direction: s for s in siblings}
        lines = [
            f"{primary.artist or 'Unknown'} — {primary.title}  (#{primary.id})",
            f"First seen on {_SVC_LABEL.get(prefix, prefix)}",
        ]
        services = ["tg", "yt", "sp"] if self._sp_enabled else ["tg", "yt"]
        for svc in services:
            if svc == prefix:
                lines.append(f"{_SVC_ICON[svc]} {_SVC_LABEL[svc]}: source")
                continue
            row = by_dir.get(f"{prefix}_to_{svc}")
            status = row.status if row else "pending"
            line = f"{_SVC_ICON[svc]} {_SVC_LABEL[svc]}: {_STATUS_ICON.get(status, '❓')} {status}"
            if svc == "yt" and row and row.yt_video_id and status in ("synced", "duplicate"):
                line += f" — https://music.youtube.com/watch?v={row.yt_video_id}"
            if svc == "sp" and row and row.sp_track_id and status in ("synced", "duplicate"):
                line += f" — https://open.spotify.com/track/{row.sp_track_id}"
            lines.append(line)
        return "\n".join(lines)
