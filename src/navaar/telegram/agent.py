from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

import structlog
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    create_sdk_mcp_server,
    query,
    tool,
)

from navaar.telegram.cards import _STATUS_ICON, _SVC_ICON, _SVC_LABEL

if TYPE_CHECKING:
    from telegram import Bot

    from navaar.db.models import Track
    from navaar.db.repository import TrackRepository
    from navaar.sync.engine import SyncEngine
    from navaar.telegram.cards import TrackCardService

logger = structlog.get_logger()

NAVAAR_SYSTEM = """\
You are Navaar's assistant — an agent running INSIDE the Navaar service pod. Navaar mirrors \
music tracks across a Telegram channel, a YouTube Music playlist, and a Spotify playlist \
(six sync directions like tg_to_yt, yt_to_tg, tg_to_sp, sp_to_tg, yt_to_sp, sp_to_yt).

You have a real shell (Bash) and file tools. For investigation or analysis — finding \
duplicates, stats, audits, ad-hoc questions — WRITE AND RUN a small Python script rather than \
guessing. python3 is available (use its sqlite3 module; there is no sqlite3 CLI).

The live SQLite database is at /data/navaar.db. Main table `tracks` columns:
  id, direction, status, artist, title, yt_video_id, sp_track_id, tg_message_id,
  card_message_id, duration_seconds, failure_reason, created_at, updated_at, synced_at.
status is one of: pending, identifying, searching, syncing, synced, failed, duplicate,
unsynced, retry_scheduled. A single song can appear as several rows (one per direction) that
share the origin's external id — that's one logical track, not duplicates. Treat the database
as READ-ONLY: query it freely, but do NOT modify it directly. To change anything, use the
navaar MCP tools below (they use the live YouTube/Spotify OAuth clients and refresh the
channel status card):
  - mcp__navaar__status(track_id)
  - mcp__navaar__unsync(track_id, platform=yt|sp|all)
  - mcp__navaar__resync(track_id, platform=yt|sp|all)
  - mcp__navaar__delete(track_id)            # removes the channel message(s), playlists, and DB row
  - mcp__navaar__delete_message(message_id)  # delete a single channel message
Prefer these tools for mutations over hand-rolled API calls. The Telegram bot token and channel
id are in the environment (NAVAAR_TELEGRAM_BOT_TOKEN, NAVAAR_TELEGRAM_CHANNEL_ID) if you need
the Bot API for something the tools don't cover.

IMPORTANT — be honest about your limits. You can only see what's in this pod: the database
(tracks Navaar has ingested since it started running) and the filesystem. A Telegram BOT cannot
read the channel's older message history, so you do NOT have a record of every message ever
posted. Never claim channel-wide certainty (e.g. "there are no duplicates in the channel");
scope your answer to the tracked data and say so plainly.

Keep your final reply concise and friendly — it is sent as a Telegram message.\
"""


def _origin(track: Track) -> str:
    return track.direction.split("_to_")[0]


def _ok(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}]}


class NavaarAgent:
    """Natural-language control backed by the Claude Agent SDK: it runs Claude Code
    (Bash + file tools) inside the pod, pointed at the Anthropic endpoint via the
    ANTHROPIC_BASE_URL / ANTHROPIC_API_KEY environment variables. Dynamic analysis is
    done by the agent writing/running scripts; Navaar's mutating operations are exposed
    as an in-process MCP server so they stay reliable (OAuth clients + card refresh).
    """

    def __init__(
        self,
        *,
        model: str,
        timeout: int,
        max_turns: int,
        workspace_dir: str,
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
        self._model = model
        self._timeout = timeout
        self._max_turns = max_turns
        self._workspace = workspace_dir
        self._bot = bot
        self._channel_id = channel_id
        self._tracks = track_repo
        self._engine = engine
        self._card = card_service
        self._yt = yt_client
        self._sp = sp_client
        self._sp_enabled = sp_enabled
        self.enabled = enabled
        # One agent session at a time — the SDK spawns a CLI subprocess per run and
        # the bot is low-traffic; serializing avoids resource spikes and DB contention.
        self._lock = asyncio.Lock()
        try:
            Path(self._workspace).mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.warning("nl_workspace_mkdir_failed", path=self._workspace, exc_info=True)
        self._mcp = self._build_mcp_server()

    # ── Orchestration ────────────────────────────────────────────────

    async def run(self, *, message_text: str, siblings: list[Track] | None = None) -> str:
        if not self.enabled:
            return ""
        prompt = self._build_prompt(message_text, siblings)
        async with self._lock:
            try:
                return await asyncio.wait_for(self._run_query(prompt), timeout=self._timeout)
            except TimeoutError:
                logger.warning("nl_agent_timeout")
                return "Sorry, that took too long so I stopped."
            except Exception:
                logger.warning("nl_agent_error", exc_info=True)
                return "Sorry, I hit an error while working on that."

    async def _run_query(self, prompt: str) -> str:
        options = ClaudeAgentOptions(
            model=self._model,
            cwd=self._workspace,
            permission_mode="bypassPermissions",
            max_turns=self._max_turns,
            setting_sources=[],
            system_prompt=NAVAAR_SYSTEM,
            mcp_servers={"navaar": self._mcp},
            env={"API_TIMEOUT_MS": str(self._timeout * 1000)},
        )
        final: str | None = None
        texts: list[str] = []
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, ResultMessage):
                if message.result:
                    final = message.result
            elif isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        texts.append(block.text)
        return final or "\n".join(texts).strip() or "Done."

    def _build_prompt(self, message_text: str, siblings: list[Track] | None) -> str:
        if siblings:
            p = siblings[0]
            ctx = (
                f'Context: the user is replying to track #{p.id} '
                f'("{p.artist or "Unknown"} — {p.title}"). "this"/"it" refers to that track.\n\n'
            )
        else:
            ctx = ""
        return f"{ctx}User request: {message_text}"

    # ── In-process MCP server (reliable Navaar mutations) ────────────

    def _build_mcp_server(self):
        _TRACK = {"type": "object", "properties": {"track_id": {"type": "integer"}},
                  "required": ["track_id"]}
        _TRACK_PLATFORM = {
            "type": "object",
            "properties": {
                "track_id": {"type": "integer"},
                "platform": {"type": "string", "enum": ["yt", "sp", "all"]},
            },
            "required": ["track_id", "platform"],
        }

        @tool("status", "Show a track's cross-platform sync status and links.", _TRACK)
        async def status(args: dict) -> dict:
            siblings = await self._resolve(args.get("track_id"))
            return _ok(self._status_text(siblings) if siblings else "No such track.")

        @tool("unsync", "Remove a track from the yt/sp/all playlist(s).", _TRACK_PLATFORM)
        async def unsync(args: dict) -> dict:
            siblings = await self._resolve(args.get("track_id"))
            if not siblings:
                return _ok("No such track.")
            return _ok(await self._do_unsync(siblings, args.get("platform", "all")))

        @tool("resync", "Re-queue a track to sync to yt/sp/all and force a sync.", _TRACK_PLATFORM)
        async def resync(args: dict) -> dict:
            siblings = await self._resolve(args.get("track_id"))
            if not siblings:
                return _ok("No such track.")
            return _ok(await self._do_resync(siblings, args.get("platform", "all")))

        @tool("delete", "Fully remove a track: channel message(s), card, playlists, DB row.", _TRACK)
        async def delete(args: dict) -> dict:
            siblings = await self._resolve(args.get("track_id"))
            if not siblings:
                return _ok("No such track.")
            return _ok(await self._do_delete(siblings))

        @tool("delete_message", "Delete a single message from the channel by id.",
              {"type": "object", "properties": {"message_id": {"type": "integer"}},
               "required": ["message_id"]})
        async def delete_message(args: dict) -> dict:
            return _ok(await self._delete_message(args.get("message_id")))

        return create_sdk_mcp_server(
            "navaar", tools=[status, unsync, resync, delete, delete_message]
        )

    # ── Core actions (shared by the MCP tools) ───────────────────────

    async def _resolve(self, track_id: object) -> list[Track] | None:
        if not isinstance(track_id, int):
            return None
        track = await self._tracks.get_track(track_id)
        return await self._tracks.get_sibling_tracks(track) if track else None

    @staticmethod
    def _ext_ids(siblings: list[Track]) -> tuple[str | None, str | None]:
        yt = next((s.yt_video_id for s in siblings if s.yt_video_id), None)
        sp = next((s.sp_track_id for s in siblings if s.sp_track_id), None)
        return yt, sp

    @staticmethod
    def _row_for(siblings: list[Track], svc: str) -> Track | None:
        return next((s for s in siblings if s.direction.endswith(f"_to_{svc}")), None)

    async def _do_unsync(self, siblings: list[Track], platform: str) -> str:
        if platform not in ("yt", "sp", "all"):
            platform = "all"
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

    async def _do_resync(self, siblings: list[Track], platform: str) -> str:
        if platform not in ("yt", "sp", "all"):
            platform = "all"
        results: list[str] = []
        for svc in [s for s in ("yt", "sp") if platform in (s, "all")]:
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

    async def _do_delete(self, siblings: list[Track]) -> str:
        await self._do_unsync(siblings, "all")
        msg_ids = {s.card_message_id for s in siblings if s.card_message_id}
        msg_ids |= {s.tg_message_id for s in siblings if s.tg_message_id}
        for mid in msg_ids:
            try:
                await self._bot.delete_message(chat_id=self._channel_id, message_id=mid)
            except Exception:
                logger.warning("nl_agent_message_delete_failed", message_id=mid, exc_info=True)
        ids = [s.id for s in siblings]
        for tid in ids:
            await self._tracks.delete_track(tid)
        return f"🗑 Deleted track #{ids[0]} (channel message, card, playlists, and record)."

    async def _delete_message(self, message_id: object) -> str:
        if not isinstance(message_id, int):
            return "Provide a numeric message_id."
        try:
            await self._bot.delete_message(chat_id=self._channel_id, message_id=message_id)
        except Exception as e:
            return f"Could not delete message {message_id}: {e}"
        return f"Deleted channel message {message_id}."

    async def _refresh_card(self, siblings: list[Track]) -> None:
        if self._card is not None:
            await self._card.refresh(siblings[0].id)

    @staticmethod
    def _headline(siblings: list[Track], verb: str) -> str:
        p = siblings[0]
        return f"{verb}: {p.artist or 'Unknown'} — {p.title} (#{p.id})"

    def _status_text(self, siblings: list[Track]) -> str:
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
