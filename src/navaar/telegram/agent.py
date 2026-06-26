from __future__ import annotations

import asyncio
import json
import re
from typing import TYPE_CHECKING

import httpx
import structlog

from navaar.telegram.cards import _STATUS_ICON, _SVC_ICON, _SVC_LABEL

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from telegram import Bot

    from navaar.db.models import Track
    from navaar.db.repository import TrackRepository
    from navaar.sync.engine import SyncEngine
    from navaar.telegram.cards import TrackCardService

logger = structlog.get_logger()

_PROMPT_HEADER = (
    "You are Navaar's assistant. Navaar mirrors music tracks across a Telegram channel, a "
    "YouTube Music playlist, and a Spotify playlist.\n\n"
    "Each turn, respond with EXACTLY ONE JSON object and NOTHING else — no prose, no markdown:\n"
    '  to use a tool:   {"tool": "<name>", "args": { ... }}\n'
    '  when finished:   {"final": "<message to the user>"}\n'
    "Never invent tool results — call the tool and wait; I send the real result back as "
    '{"tool_result": ...}. Keep the final message short and friendly.\n\n'
    "Tools:\n"
)

# Tool descriptions, emitted only for registered tools (shell is gated).
_TOOL_DESCRIPTIONS = {
    "shell": "shell(command): run a shell command inside the Navaar pod; returns combined output.",
    "sql": (
        "sql(query): run a read-only SELECT over the SQLite DB and get rows. Main table "
        "tracks(id, direction, status, artist, title, yt_video_id, sp_track_id, "
        "tg_message_id, card_message_id, duration_seconds, failure_reason, created_at). "
        "direction is e.g. 'tg_to_yt'; status is pending/synced/failed/duplicate/unsynced."
    ),
    "status": "status(track_id?): show a track's cross-platform sync status and links.",
    "unsync": "unsync(track_id?, platform): remove a track from yt/sp/all playlists.",
    "resync": "resync(track_id?, platform): re-queue a track to sync to yt/sp/all.",
    "delete": "delete(track_id?): remove a track from all playlists and forget it.",
    "find_duplicates": "find_duplicates(): list songs that appear more than once in the channel.",
}
_TOOL_ORDER = ["status", "unsync", "resync", "delete", "find_duplicates", "sql", "shell"]


def _origin(track: Track) -> str:
    return track.direction.split("_to_")[0]


class NavaarAgent:
    """Natural-language control as a bounded, in-pod tool loop. The configured
    OpenAI-compatible endpoint is a Claude Code shim that doesn't emit OpenAI
    ``tool_calls``, so we drive a text protocol ourselves: the model emits one JSON
    object per turn ({"tool",...} or {"final",...}); this class executes the tool in
    the pod and feeds the real result back. The model decides; the pod acts.
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
        shell_enabled: bool = False,
        max_iterations: int = 8,
        shell_timeout: int = 30,
        tool_output_limit: int = 4000,
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
        self._shell_enabled = shell_enabled
        self._max_iter = max_iterations
        self._shell_timeout = shell_timeout
        self._out_limit = tool_output_limit

        self._tools: dict[str, Callable[[dict, int | None], Awaitable[str]]] = {
            "status": self._tool_status,
            "unsync": self._tool_unsync,
            "resync": self._tool_resync,
            "retry": self._tool_resync,
            "delete": self._tool_delete,
            "find_duplicates": self._tool_find_duplicates,
            "sql": self._tool_sql,
        }
        if shell_enabled:
            self._tools["shell"] = self._tool_shell

    # ── Loop ─────────────────────────────────────────────────────────

    async def run(self, *, message_text: str, siblings: list[Track] | None = None) -> str:
        if not self.enabled:
            return ""
        ctx_id = siblings[0].id if siblings else None
        messages = [
            {"role": "system", "content": self._system_prompt(siblings)},
            {"role": "user", "content": message_text},
        ]
        for _ in range(self._max_iter):
            try:
                content = await self._chat(messages)
            except Exception:
                logger.warning("nl_agent_request_failed", exc_info=True)
                return "Sorry, I couldn't reach my language model just now."

            obj = self._first_json(content)
            if obj is None:
                messages.append({"role": "assistant", "content": content})
                messages.append({"role": "user", "content":
                    'Respond with exactly one JSON object: {"tool",...} or {"final",...}.'})
                continue
            if "final" in obj:
                return str(obj["final"]).strip() or "Done."

            tool = obj.get("tool")
            args = obj.get("args") if isinstance(obj.get("args"), dict) else {}
            if tool not in self._tools:
                result = f"Unknown tool '{tool}'. Available: {', '.join(self._tools)}."
            else:
                logger.info("nl_tool_call", tool=tool, args=args)
                try:
                    result = await self._tools[tool](args, ctx_id)
                except Exception as e:
                    logger.warning("nl_tool_error", tool=tool, exc_info=True)
                    result = f"Tool error: {e}"

            messages.append({"role": "assistant", "content": json.dumps(obj)})
            messages.append({"role": "user", "content":
                json.dumps({"tool_result": str(result)[:self._out_limit]}, default=str)})

        return "Sorry, I couldn't finish that — it took too many steps."

    def _system_prompt(self, siblings: list[Track] | None) -> str:
        names = [n for n in _TOOL_ORDER if n in self._tools]
        tools_desc = "\n".join(f"- {_TOOL_DESCRIPTIONS[n]}" for n in names)
        if siblings:
            p = siblings[0]
            ctx = (
                f'The user is replying to track #{p.id} ("{p.artist or "Unknown"} — '
                f'{p.title}"). "this"/"it" means that track; omit track_id to use it.'
            )
        else:
            ctx = "No specific track is in context; use sql/find_duplicates or a #id."
        return _PROMPT_HEADER + tools_desc + "\n\n" + ctx

    async def _chat(self, messages: list[dict]) -> str:
        payload = {"model": self._model, "max_tokens": 1024, "messages": messages}
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                f"{self._base_url}/chat/completions", json=payload, headers=headers
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]

    @staticmethod
    def _first_json(content: str) -> dict | None:
        """Extract the first balanced JSON object, ignoring any trailing content
        (the shim tends to hallucinate a result + final after its tool call)."""
        text = content.strip()
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
            text = re.sub(r"\n?```$", "", text).strip()
        start = text.find("{")
        if start == -1:
            return None
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            c = text[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        return None
                    return obj if isinstance(obj, dict) else None
        return None

    # ── Tools ────────────────────────────────────────────────────────

    async def _resolve(self, args: dict, ctx_id: int | None) -> list[Track] | None:
        tid = args.get("track_id")
        tid = tid if isinstance(tid, int) else ctx_id
        if tid is None:
            return None
        track = await self._tracks.get_track(tid)
        return await self._tracks.get_sibling_tracks(track) if track else None

    async def _tool_status(self, args: dict, ctx_id: int | None) -> str:
        siblings = await self._resolve(args, ctx_id)
        if not siblings:
            return "No such track (give a track_id or reply to a track)."
        return self._context_text(siblings)

    async def _tool_unsync(self, args: dict, ctx_id: int | None) -> str:
        siblings = await self._resolve(args, ctx_id)
        if not siblings:
            return "No such track."
        return await self._do_unsync(siblings, self._platform(args))

    async def _tool_resync(self, args: dict, ctx_id: int | None) -> str:
        siblings = await self._resolve(args, ctx_id)
        if not siblings:
            return "No such track."
        return await self._do_resync(siblings, self._platform(args))

    async def _tool_delete(self, args: dict, ctx_id: int | None) -> str:
        siblings = await self._resolve(args, ctx_id)
        if not siblings:
            return "No such track."
        return await self._do_delete(siblings)

    async def _tool_find_duplicates(self, args: dict, ctx_id: int | None) -> str:
        return await self._find_duplicates()

    async def _tool_sql(self, args: dict, ctx_id: int | None) -> str:
        query = args.get("query") or args.get("sql") or ""
        if not query:
            return "No query."
        try:
            rows = await self._tracks.run_select(query)
        except Exception as e:
            return f"SQL error: {e}"
        return json.dumps(rows, default=str)

    async def _tool_shell(self, args: dict, ctx_id: int | None) -> str:
        command = args.get("command") or args.get("cmd") or ""
        if not command:
            return "No command."
        proc = await asyncio.create_subprocess_exec(
            "sh", "-c", command,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=self._shell_timeout)
        except TimeoutError:
            proc.kill()
            return f"Command timed out after {self._shell_timeout}s."
        text = out.decode(errors="replace")
        return text or "(no output)"

    @staticmethod
    def _platform(args: dict) -> str:
        p = args.get("platform")
        return p if p in ("yt", "sp", "all") else "all"

    # ── Core actions ─────────────────────────────────────────────────

    @staticmethod
    def _ext_ids(siblings: list[Track]) -> tuple[str | None, str | None]:
        yt = next((s.yt_video_id for s in siblings if s.yt_video_id), None)
        sp = next((s.sp_track_id for s in siblings if s.sp_track_id), None)
        return yt, sp

    @staticmethod
    def _row_for(siblings: list[Track], svc: str) -> Track | None:
        return next((s for s in siblings if s.direction.endswith(f"_to_{svc}")), None)

    async def _do_unsync(self, siblings: list[Track], platform: str) -> str:
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

    @staticmethod
    def _norm(s: str | None) -> str:
        return " ".join((s or "").strip().lower().split())

    async def _find_duplicates(self, limit: int = 30) -> str:
        tracks = await self._tracks.get_channel_tracks()
        groups: dict[tuple[str, str], list] = {}
        for t in tracks:
            groups.setdefault((self._norm(t.artist), self._norm(t.title)), []).append(t)
        dups = sorted(
            (g for g in groups.values() if len(g) > 1),
            key=lambda g: (-len(g), g[0].id),
        )
        if not dups:
            return f"No duplicate songs found among {len(tracks)} tracks in the channel."
        lines = [f"Found {len(dups)} duplicated song(s) among {len(tracks)} tracks:"]
        for g in dups[:limit]:
            first = g[0]
            refs = ", ".join(f"#{t.id} (msg {t.tg_message_id})" for t in g)
            lines.append(f"• {first.artist or 'Unknown'} — {first.title} ×{len(g)} — {refs}")
        if len(dups) > limit:
            lines.append(f"… and {len(dups) - limit} more")
        return "\n".join(lines)

    # ── Presentation ─────────────────────────────────────────────────

    @staticmethod
    def _headline(siblings: list[Track], verb: str) -> str:
        p = siblings[0]
        return f"{verb}: {p.artist or 'Unknown'} — {p.title} (#{p.id})"

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
