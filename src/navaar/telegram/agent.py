from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import structlog
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    create_sdk_mcp_server,
    delete_session,
    get_session_info,
    query,
    tool,
)

from navaar.telegram.cards import _STATUS_ICON, _SVC_ICON, _SVC_LABEL

if TYPE_CHECKING:
    from telegram import Bot

    from navaar.db.models import Track
    from navaar.db.repository import SyncStateRepository, TrackRepository
    from navaar.sync.engine import SyncEngine
    from navaar.telegram.cards import TrackCardService

logger = structlog.get_logger()

_COMPACT_PROMPT = (
    "Summarize our conversation so far — the key facts, decisions, track ids, and any state "
    "worth remembering — as a tight bulleted note. Reply with ONLY the summary, no preamble."
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _ago(iso: str | None) -> str:
    if not iso:
        return "just now"
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return "?"
    secs = int((datetime.now(UTC) - dt).total_seconds())
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def _tok(usage: dict, key: str) -> int:
    v = usage.get(key) if isinstance(usage, dict) else None
    return v if isinstance(v, int) else 0

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
        sync_state: SyncStateRepository,
        engine: SyncEngine | None,
        card_service: TrackCardService | None,
        yt_client: object,
        sp_client: object | None,
        sp_enabled: bool,
        enabled: bool = True,
        context_window: int = 200000,
    ) -> None:
        self._model = model
        self._timeout = timeout
        self._max_turns = max_turns
        self._workspace = workspace_dir
        self._bot = bot
        self._channel_id = channel_id
        self._tracks = track_repo
        self._state = sync_state
        self._engine = engine
        self._card = card_service
        self._yt = yt_client
        self._sp = sp_client
        self._sp_enabled = sp_enabled
        self.enabled = enabled
        self._context_window = context_window
        # One shared conversation across the channel and all DMs. The session id is
        # persisted in SyncState (and the transcript on the /data PVC) so memory
        # survives restarts.
        self._session_id: str | None = None
        self._loaded = False
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
        await self._load()
        prompt = self._build_prompt(message_text, siblings)
        async with self._lock:
            try:
                text, meta = await asyncio.wait_for(
                    self._run_query(prompt, resume=self._session_id), timeout=self._timeout
                )
            except TimeoutError:
                logger.warning("nl_agent_timeout")
                return "Sorry, that took too long so I stopped."
            except Exception:
                logger.warning("nl_agent_error", exc_info=True)
                # A stale/missing resumed session must not wedge the bot — start fresh.
                if self._session_id is not None:
                    self._session_id = None
                    await self._save_session(None)
                return "Sorry, I hit an error while working on that."
            await self._remember(meta)
            return text

    async def _run_query(self, prompt: str, resume: str | None = None) -> tuple[str, dict]:
        options = ClaudeAgentOptions(
            model=self._model,
            cwd=self._workspace,
            permission_mode="bypassPermissions",
            max_turns=self._max_turns,
            setting_sources=[],
            resume=resume,
            system_prompt=NAVAAR_SYSTEM,
            mcp_servers={"navaar": self._mcp},
            env={"API_TIMEOUT_MS": str(self._timeout * 1000)},
        )
        final: str | None = None
        texts: list[str] = []
        meta: dict = {"session_id": None, "usage": {}, "cost": 0.0, "turns": 0}
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, ResultMessage):
                if message.result:
                    final = message.result
                meta["session_id"] = message.session_id
                meta["usage"] = message.usage or {}
                meta["cost"] = message.total_cost_usd or 0.0
                meta["turns"] = message.num_turns or 0
            elif isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        texts.append(block.text)
        return final or "\n".join(texts).strip() or "Done.", meta

    # ── Session state (one shared brain, persisted) ──────────────────

    async def _load(self) -> None:
        if self._loaded:
            return
        self._session_id = (await self._state.get("agent_session_id")) or None
        self._loaded = True

    async def _save_session(self, session_id: str | None) -> None:
        await self._state.set("agent_session_id", session_id or "")

    async def _load_usage(self) -> dict:
        u = await self._state.get_json("agent_usage")
        return u if isinstance(u, dict) else {}

    async def _remember(self, meta: dict) -> None:
        sid = meta.get("session_id")
        if sid and sid != self._session_id:
            self._session_id = sid
        await self._save_session(self._session_id)
        usage = meta.get("usage") or {}
        prev = await self._load_usage()
        snap = {
            "started_at": prev.get("started_at") or _now_iso(),
            "messages": int(prev.get("messages", 0)) + 1,
            "last_input": _tok(usage, "input_tokens"),
            "last_output": _tok(usage, "output_tokens"),
            "last_cache_read": _tok(usage, "cache_read_input_tokens"),
            "last_cache_creation": _tok(usage, "cache_creation_input_tokens"),
            "cost_total": round(float(prev.get("cost_total", 0.0)) + float(meta.get("cost") or 0.0), 4),
            "updated_at": _now_iso(),
        }
        await self._state.set_json("agent_usage", snap)

    # ── Management (slash commands) ───────────────────────────────────

    async def reset(self) -> str:
        await self._load()
        async with self._lock:
            old = self._session_id
            if old:
                try:
                    await asyncio.to_thread(delete_session, old, self._workspace)
                except Exception:
                    logger.warning("nl_session_delete_failed", exc_info=True)
            self._session_id = None
            await self._save_session(None)
            await self._state.set_json("agent_usage", {})
        logger.info("nl_agent_reset")
        return "🧠 Memory reset — starting a fresh conversation."

    async def context_info(self) -> str:
        await self._load()
        if not self._session_id:
            return "🧠 No active conversation yet — send me a message and I'll start one."
        usage = await self._load_usage()
        ctx = (usage.get("last_input") or 0) + (usage.get("last_cache_read") or 0)
        pct = round(ctx / self._context_window * 100, 1) if self._context_window else 0.0
        summary = ""
        try:
            info = await asyncio.to_thread(get_session_info, self._session_id, self._workspace)
            if info is not None:
                summary = getattr(info, "summary", "") or getattr(info, "first_prompt", "") or ""
        except Exception:
            logger.warning("nl_session_info_failed", exc_info=True)
        lines = [
            "🧠 Conversation context",
            f"• Messages: {usage.get('messages', 0)} · started {_ago(usage.get('started_at'))}",
            f"• Context: ~{ctx / 1000:.1f}k tokens (~{pct}% of {self._context_window // 1000}k)",
            f"• Last turn: {(usage.get('last_input') or 0) / 1000:.1f}k in / "
            f"{(usage.get('last_output') or 0) / 1000:.1f}k out · "
            f"{(usage.get('last_cache_read') or 0) / 1000:.1f}k cached",
            f"• Cost so far: ${usage.get('cost_total', 0.0):.2f}",
            "• Autocompact: on (auto-summarizes near the limit)",
        ]
        if summary:
            lines.append(f"• Topic: {summary[:160]}")
        lines.append("Use /reset to wipe or /compact to shrink.")
        return "\n".join(lines)

    async def compact(self) -> str:
        await self._load()
        if not self._session_id:
            return "🧠 Nothing to compact yet."
        async with self._lock:
            old = self._session_id
            try:
                summary, _ = await asyncio.wait_for(
                    self._run_query(_COMPACT_PROMPT, resume=old), timeout=self._timeout
                )
            except Exception:
                logger.warning("nl_compact_summary_failed", exc_info=True)
                return "Sorry, I couldn't compact just now."
            try:
                await asyncio.to_thread(delete_session, old, self._workspace)
            except Exception:
                logger.warning("nl_session_delete_failed", exc_info=True)
            seed = (
                "This is a compacted continuation of an earlier conversation. Here is the "
                f"summary of what came before:\n\n{summary}\n\nAcknowledge in one short sentence."
            )
            try:
                _, meta = await asyncio.wait_for(
                    self._run_query(seed, resume=None), timeout=self._timeout
                )
                self._session_id = meta.get("session_id")
            except Exception:
                logger.warning("nl_compact_reseed_failed", exc_info=True)
                self._session_id = None
            await self._save_session(self._session_id)
            await self._state.set_json("agent_usage", {})
        logger.info("nl_agent_compacted")
        return "🗜 Compacted — summarized the conversation into a smaller, fresh context."

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
