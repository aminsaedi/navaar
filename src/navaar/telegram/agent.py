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
    delete_session,
    get_session_info,
    query,
)

if TYPE_CHECKING:
    from navaar.db.models import Track
    from navaar.db.repository import SyncStateRepository

logger = structlog.get_logger()

NAVAAR_SYSTEM = (
    "You are Navaar's assistant, running inside the Navaar service pod with a real shell and "
    "file tools (Bash, Read, Write, Edit, Glob, Grep). The CLAUDE.md in your working directory "
    "documents the environment — the database, credentials, source code, and how to manage "
    "tracks. Follow it. For any question or task, investigate with real data: write a script, "
    "run it, check the output, iterate — never guess. Be honest about your limits and keep "
    "replies concise and friendly, since they are sent as Telegram messages."
)

# Written to <workspace>/CLAUDE.md on startup so Claude Code loads it as project memory
# (options use setting_sources=["project"]). This is the agent's whole operating manual —
# there are intentionally NO custom Navaar tools; the agent does everything via the shell.
_NAVAAR_CLAUDE_MD = """# Navaar — operating context for the assistant

You are an assistant embedded **inside the Navaar service pod**. Navaar mirrors music tracks
across a Telegram channel, a YouTube Music playlist, and a Spotify playlist. There are six sync
directions: `tg_to_yt`, `yt_to_tg`, `tg_to_sp`, `sp_to_tg`, `yt_to_sp`, `sp_to_yt`.

## How to work
- You have a real shell and file tools. There are **no built-in Navaar commands** — you do
  everything yourself by writing and running scripts (prefer Python; `python3` is available).
- Always work from real data: query the database / call the APIs, check the output, validate,
  then answer. Do not guess or rely on memory of past runs.
- Example — "list duplicate tracks": open `/data/navaar.db` with python's `sqlite3`, write a
  query/script that groups by normalized artist+title (and check `tg_file_unique_id` and
  `yt_video_id`), run it, sanity-check, then report.

## Database — `/data/navaar.db` (SQLite; use python's `sqlite3`, there is no `sqlite3` CLI)
Table `tracks`:
  `id, direction, status, artist, title, yt_video_id, sp_track_id, yt_set_video_id,
   tg_message_id, card_message_id, duration_seconds, identification_method, failure_reason,
   retry_count, max_retries, created_at, updated_at, synced_at`
- `status`: pending, identifying, searching, syncing, synced, failed, duplicate, unsynced,
  retry_scheduled.
- A *logical track* is several rows (one per direction) that share the origin's external id —
  the same song across directions is **not** a duplicate.
Other tables: `sync_state` (key/value; your own conversation session id lives under
`agent_session_id` — don't touch it), `sync_log` (event history).
Treat the DB as the source of truth. You MAY modify it for management tasks, but be surgical:
always use an explicit `WHERE id = ...`, and copy the file first if a change is risky.

## Navaar's own source — `/app/src/navaar` (read it to learn how anything works)
The Python env `/app/.venv` has `ytmusicapi`, `spotipy`, `yt-dlp`, `sqlalchemy`, etc. You can
import Navaar's clients, e.g. `from navaar.spotify.client import SpotifyClient`, or just read
the files to see exact API calls.

## Credentials & services
- YouTube Music: OAuth token file `/data/oauth.json`; client id/secret in env
  `NAVAAR_YTMUSIC_CLIENT_ID` / `NAVAAR_YTMUSIC_CLIENT_SECRET`; playlist id env
  `NAVAAR_YTMUSIC_PLAYLIST_ID`. Removing a playlist entry needs its playlistItem id
  (`setVideoId`) — see `/app/src/navaar/ytmusic/client.py`.
- Spotify: cached creds `/data/.spotify_cache`; playlist id env `NAVAAR_SPOTIFY_PLAYLIST_ID`;
  use `spotipy` — see `/app/src/navaar/spotify/client.py`.
- Telegram: bot token env `NAVAAR_TELEGRAM_BOT_TOKEN`, channel id `NAVAAR_TELEGRAM_CHANNEL_ID`.
  The bot is a channel admin — use the Bot API via `curl` (e.g. `deleteMessage`).

## What management actions mean
- **unsync** = remove the track from the platform's playlist AND set the relevant
  `{origin}_to_{platform}` row's `status` to `unsynced` (so the sync loops won't re-add it).
- **resync** = set that row's `status` to `retry_scheduled` (the loops re-process it).
- **delete** = remove from both playlists + delete the channel message(s) (`tg_message_id` and
  `card_message_id`) via the Bot API + delete the DB rows for the logical track.

## Honesty
You only see what's in this pod: the database (tracks Navaar ingested since it started) and the
filesystem. A Telegram **bot cannot read the channel's older message history**, so you do not
have a record of every message ever posted. Never claim channel-wide certainty (e.g. "there are
no duplicates in the channel") — scope your answer to the tracked data and say so.

## Replies
Keep final replies short and friendly for Telegram; avoid large markdown tables.
"""

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


class NavaarAgent:
    """Natural-language control backed by the Claude Agent SDK: it runs Claude Code (Bash +
    file tools) inside the pod, pointed at the Anthropic endpoint via the ANTHROPIC_BASE_URL /
    ANTHROPIC_API_KEY environment variables. There are no custom tools — the agent manages
    Navaar entirely by writing and running its own scripts, guided by the CLAUDE.md written
    into its workspace. One shared, persisted conversation backs /reset, /context, /compact.
    """

    def __init__(
        self,
        *,
        model: str,
        timeout: int,
        max_turns: int,
        workspace_dir: str,
        sync_state: SyncStateRepository,
        enabled: bool = True,
        context_window: int = 200000,
    ) -> None:
        self._model = model
        self._timeout = timeout
        self._max_turns = max_turns
        self._workspace = workspace_dir
        self._state = sync_state
        self.enabled = enabled
        self._context_window = context_window
        self._lock = asyncio.Lock()
        try:
            Path(self._workspace).mkdir(parents=True, exist_ok=True)
            Path(self._workspace, "CLAUDE.md").write_text(_NAVAAR_CLAUDE_MD, encoding="utf-8")
        except OSError:
            logger.warning("nl_workspace_init_failed", path=self._workspace, exc_info=True)
        # One shared conversation across the channel and all DMs; the session id is persisted
        # in SyncState (and the transcript on the /data PVC) so memory survives restarts.
        self._session_id: str | None = None
        self._loaded = False

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
                if self._session_id is not None:  # stale/missing session must not wedge the bot
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
            setting_sources=["project"],  # load CLAUDE.md from the workspace
            resume=resume,
            system_prompt=NAVAAR_SYSTEM,
            env={"API_TIMEOUT_MS": str(self._timeout * 1000)},
        )
        final: str | None = None
        texts: list[str] = []
        meta: dict = {"session_id": None, "usage": {}, "cost": 0.0, "turns": 0}
        truncated = False
        try:
            async for message in query(prompt=prompt, options=options):
                # session_id rides on the init system message too, so capture it from
                # any message — on a max-turns abort the ResultMessage never arrives.
                sid = getattr(message, "session_id", None)
                if sid:
                    meta["session_id"] = sid
                if isinstance(message, ResultMessage):
                    if message.result:
                        final = message.result
                    meta["usage"] = message.usage or {}
                    meta["cost"] = message.total_cost_usd or 0.0
                    meta["turns"] = message.num_turns or 0
                elif isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            texts.append(block.text)
        except Exception as e:
            # The SDK *raises* when Claude Code stops at the turn limit. That's not a
            # real failure — the agent has usually done useful work and produced text
            # along the way. Surface that partial progress instead of a generic error,
            # and keep the session so the user can just say "continue".
            if "maximum number of turns" not in str(e).lower():
                raise
            truncated = True
            logger.warning("nl_agent_max_turns", max_turns=self._max_turns)
        body = final or "\n".join(texts).strip() or "Done."
        if truncated:
            body += (
                f"\n\n⚠️ I stopped at my {self._max_turns}-turn limit — the work above may be "
                "partial. Reply “continue” and I'll pick up where I left off."
            )
        return body, meta

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
