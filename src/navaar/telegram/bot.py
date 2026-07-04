from __future__ import annotations

import asyncio
import contextlib
import html
import re
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import structlog
from telegram import (
    BotCommand,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from navaar.db.repository import SyncLogRepository, SyncStateRepository, TrackRepository
from navaar.metrics import RETRIES_TOTAL, TRACKS_DISCOVERED
from navaar.services import SVC_ICON, SVC_LABEL, is_sync_caption
from navaar.sync.fanout import FanOut

if TYPE_CHECKING:
    from navaar.spotify.client import SpotifyClient
    from navaar.sync.engine import SyncEngine
    from navaar.ytmusic.client import YTMusicClient

logger = structlog.get_logger()

# Status indicators
_S = {
    "pending": "\u23f3",       # hourglass
    "identifying": "\U0001f50d", # magnifying glass
    "searching": "\U0001f50e",   # magnifying glass right
    "syncing": "\u2699\ufe0f",   # gear
    "synced": "\u2705",          # green check
    "failed": "\u274c",          # red X
    "duplicate": "\U0001f501",   # repeat
    "retry_scheduled": "\U0001f504", # arrows counterclockwise
    "unsynced": "\U0001f6ab",    # no-entry (removed from playlist)
}

_DIR = {
    "tg_to_yt": "\U0001f4e4 TG \u2192 YT",
    "yt_to_tg": "\U0001f4e5 YT \u2192 TG",
    "tg_to_sp": "\U0001f4e4 TG \u2192 SP",
    "sp_to_tg": "\U0001f4e5 SP \u2192 TG",
    "yt_to_sp": "\U0001f3b5 YT \u2192 SP",
    "sp_to_yt": "\U0001f3b5 SP \u2192 YT",
}


def _track_line(t, verbose: bool = False) -> str:
    icon = _S.get(t.status, "\u2753")
    artist = html.escape(t.artist or "Unknown")
    title = html.escape(t.title)
    line = f"{icon} <code>#{t.id}</code> {artist} \u2014 {title}"
    if verbose:
        line += f"\n   {_DIR.get(t.direction, t.direction)} | {t.status}"
        if t.yt_video_id:
            line += f" | <code>{t.yt_video_id}</code>"
        if t.sp_track_id:
            line += f" | <code>{t.sp_track_id}</code>"
        if t.failure_reason:
            line += f"\n   Reason: <i>{html.escape(t.failure_reason[:80])}</i>"
    return line


def _ago(dt: datetime | None) -> str:
    if not dt:
        return "never"
    now = datetime.now(UTC)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    delta = now - dt
    secs = int(delta.total_seconds())
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


class NavaarBot:
    def __init__(
        self,
        token: str,
        channel_id: int,
        admin_user_ids: list[int],
        track_repo: TrackRepository,
        sync_log: SyncLogRepository,
        sync_state: SyncStateRepository | None = None,
        sync_engine: SyncEngine | None = None,
        yt_client: YTMusicClient | None = None,
        sp_client: SpotifyClient | None = None,
    ) -> None:
        self._token = token
        self._channel_id = channel_id
        self._admin_ids = set(admin_user_ids)
        self._tracks = track_repo
        self._log = sync_log
        self._state = sync_state
        self._engine = sync_engine
        self._yt = yt_client
        self._sp = sp_client
        self._sp_enabled = sp_client is not None
        self._fanout = FanOut(track_repo, sp_enabled=self._sp_enabled)
        self._card = None
        self._agent = None
        self._bot_username: str | None = None
        self._app: Application | None = None
        self._start_time = time.time()

    def set_sync_engine(self, engine: SyncEngine) -> None:
        self._engine = engine

    def set_card_service(self, card_service: object) -> None:
        self._card = card_service

    def set_agent(self, agent: object) -> None:
        self._agent = agent

    def _is_admin(self, update: Update) -> bool:
        user = update.effective_user
        return user is not None and user.id in self._admin_ids

    async def _reply(self, update: Update, text: str, **kwargs) -> None:
        msg = update.message or update.callback_query.message
        await msg.reply_text(text, parse_mode="HTML", disable_web_page_preview=True, **kwargs)

    # ── Channel post handler ─────────────────────────────────────────

    async def _handle_channel_post(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        message = update.channel_post
        if not message or not message.audio:
            return
        if message.chat_id != self._channel_id:
            return

        # Ignore the bot's own YT/SP->TG uploads. Channel posts are attributed to the
        # channel (from_user is None), so the bot can't recognise its uploads by
        # sender — it recognises them by the structured sync caption (marker + `· #id`),
        # which travels with the message and works even before the upload's DB row is
        # committed. Matching the full structure (not a bare substring) avoids dropping
        # a human post that merely happens to mention the marker phrase.
        if is_sync_caption(message.caption):
            logger.debug("tg_ignoring_own_upload", message_id=message.message_id)
            return
        if message.from_user and message.from_user.id == context.bot.id:
            logger.debug("tg_ignoring_own_message", message_id=message.message_id)
            return

        audio = message.audio
        logger.info(
            "tg_audio_received",
            message_id=message.message_id,
            file_id=audio.file_id,
            title=audio.title,
            performer=audio.performer,
            file_name=audio.file_name,
        )

        # Dedup by file_unique_id
        existing = await self._tracks.get_track_by_tg_file_unique_id(audio.file_unique_id)
        if existing:
            logger.info("tg_duplicate_file", file_unique_id=audio.file_unique_id)
            return

        # Dedup by message_id
        existing_msg = await self._tracks.get_track_by_tg_message_id(message.message_id)
        if existing_msg:
            logger.debug("tg_message_already_tracked", message_id=message.message_id)
            return

        title = audio.title or audio.file_name or "Unknown"
        # Attribution, when the channel has "Sign messages" enabled: channel posts
        # carry author_signature (the poster's name) even though from_user is None.
        added_by = message.author_signature or None

        # Create primary tg_to_yt track
        track = await self._tracks.create_track(
            direction="tg_to_yt",
            status="pending",
            title=title,
            artist=audio.performer,
            tg_message_id=message.message_id,
            tg_file_id=audio.file_id,
            tg_file_unique_id=audio.file_unique_id,
            duration_seconds=audio.duration,
            added_by=added_by,
        )
        TRACKS_DISCOVERED.labels(direction="tg_to_yt").inc()
        await self._log.log(
            "track_discovered",
            track_id=track.id,
            direction="tg_to_yt",
            details={
                "message_id": message.message_id,
                "title": title,
                "performer": audio.performer,
            },
        )
        logger.info("tg_track_created", track_id=track.id, title=title)

        # Fan-out to the other targets (Spotify), with consistent dedup.
        await self._fanout.from_telegram(
            tg_file_id=audio.file_id,
            title=title,
            artist=audio.performer,
            duration=audio.duration,
        )

        # Reply with the initial status card (targets pending). It then edits
        # itself in place as each direction finishes syncing.
        if self._card is not None:
            await self._card.refresh(track.id)

    # ── Natural-language control ─────────────────────────────────────

    def _strip_mention(self, text: str) -> str:
        if self._bot_username:
            text = re.sub(
                rf"@{re.escape(self._bot_username)}", "", text, flags=re.IGNORECASE
            )
        return text.strip()

    async def _handle_channel_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """A text post in the channel that @-mentions the bot → natural-language
        action. If it replies to a track's audio message or status card, that track
        is the context ("this"/"it"); otherwise it's a channel-wide request (e.g.
        "list duplicates"). Posting rights to the channel are the gate."""
        if self._agent is None or not getattr(self._agent, "enabled", False):
            return
        message = update.channel_post
        if not message or not message.text or message.chat_id != self._channel_id:
            return
        if not self._bot_username or f"@{self._bot_username}".lower() not in message.text.lower():
            return

        siblings = None
        if message.reply_to_message:
            track = await self._tracks.get_logical_track_by_message_id(
                message.reply_to_message.message_id
            )
            if track:
                siblings = await self._tracks.get_sibling_tracks(track)
        text = self._strip_mention(message.text)
        ctrl = self._control_command(text)
        if ctrl is not None:
            await message.reply_text(await ctrl(), disable_web_page_preview=True)
            return

        # Feedback in the channel: Telegram does NOT render typing indicators in
        # channels, so post an editable placeholder and swap in the real answer when
        # the agent finishes (it can run up to nl_request_timeout seconds). Without
        # this the channel looks dead and friends re-post. Best-effort throughout.
        placeholder = None
        with contextlib.suppress(Exception):
            placeholder = await message.reply_text(
                "\U0001f916 On it…", disable_web_page_preview=True
            )
        result = await self._agent.run(message_text=text, siblings=siblings)
        edited = False
        if placeholder is not None:
            with contextlib.suppress(Exception):
                await placeholder.edit_text(result, disable_web_page_preview=True)
                edited = True
            if not edited:
                # Edit failed (e.g. result too long) — drop the stale placeholder so it
                # doesn't linger beside the real reply.
                with contextlib.suppress(Exception):
                    await placeholder.delete()
        if not edited:
            # Suppressed so an oversized reply can't propagate out of the handler.
            with contextlib.suppress(Exception):
                await message.reply_text(result, disable_web_page_preview=True)

    async def _handle_dm_message(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """An admin DM (non-command text) → natural-language action. The target track
        comes from an id in the message or defaults to the most recent."""
        if self._agent is None or not getattr(self._agent, "enabled", False):
            return
        if not self._is_admin(update):
            return
        message = update.message
        if not message or not message.text:
            return
        await self._run_agent_with_feedback(message, context, message.text, siblings=None)

    async def _run_agent_with_feedback(
        self, message, context: ContextTypes.DEFAULT_TYPE, text: str, *, siblings
    ) -> None:
        """Run the agent for a DM with live feedback: post an immediate "…handling your
        request" placeholder, keep a "typing…" chat action alive while it works, then
        delete the placeholder and send the real reply. All feedback is best-effort —
        it can never block or break the actual answer."""
        chat_id = message.chat_id
        typing = asyncio.create_task(self._keep_typing(context, chat_id))
        placeholder = None
        try:
            with contextlib.suppress(Exception):
                placeholder = await message.reply_text(
                    "\U0001f916 Navaar agent is handling your request…"
                )
            result = await self._agent.run(message_text=text, siblings=siblings)
        finally:
            typing.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await typing
            if placeholder is not None:
                with contextlib.suppress(Exception):
                    await placeholder.delete()
        await message.reply_text(result, disable_web_page_preview=True)

    async def _keep_typing(self, context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
        """Re-send the 'typing…' chat action every few seconds until cancelled — Telegram
        clears the indicator after ~5s, so a long agent turn needs it refreshed."""
        try:
            while True:
                with contextlib.suppress(Exception):
                    await context.bot.send_chat_action(
                        chat_id=chat_id, action=ChatAction.TYPING
                    )
                await asyncio.sleep(4)
        except asyncio.CancelledError:
            pass

    # ── Agent conversation controls (/reset, /context, /compact) ─────

    def _control_command(self, text: str):
        """Map a control command (with or without a leading slash) to the agent
        coroutine that handles it, or None if the text isn't a control command.
        Lets the channel @mention path reuse the same controls as the DM commands."""
        if self._agent is None:
            return None
        word = text.strip().lower().lstrip("/").split()[0] if text.strip() else ""
        return {
            "reset": self._agent.reset,
            "context": self._agent.context_info,
            "usage": self._agent.context_info,
            "compact": self._agent.compact,
        }.get(word)

    async def _cmd_reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_admin(update):
            return
        await self._reply_agent_control(update, self._agent.reset if self._agent else None)

    async def _cmd_context(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_admin(update):
            return
        await self._reply_agent_control(update, self._agent.context_info if self._agent else None)

    async def _cmd_compact(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_admin(update):
            return
        await self._reply_agent_control(update, self._agent.compact if self._agent else None)

    async def _reply_agent_control(self, update: Update, fn) -> None:
        if fn is None:
            await update.message.reply_text("❌ The agent isn't enabled.")
            return
        # Plain text (not HTML): the readout can contain arbitrary session-summary text.
        await update.message.reply_text(await fn(), disable_web_page_preview=True)

    # ── /start, /help ────────────────────────────────────────────────

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_admin(update):
            return
        await self._cmd_help(update, context)

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_admin(update):
            return
        sp_cmds = ""
        if self._sp_enabled:
            sp_cmds = (
                "/sync sp \u2014 Force SP\u2192TG + SP\u2192YT sync\n"
                "/retry sp \u2014 Retry all failed SP tracks\n"
                "/search_sp &lt;query&gt; \u2014 Search Spotify\n"
            )
        text = (
            "<b>\U0001f3b5 Navaar \u2014 Bot Commands</b>\n"
            "\n"
            "<b>Monitoring</b>\n"
            "/status \u2014 Live sync status dashboard\n"
            "/stats \u2014 Aggregate statistics\n"
            "/health \u2014 Tracks missing on some platforms\n"
            "/digest [days] \u2014 Recap of recent additions\n"
            "/queue \u2014 Pending tracks waiting to sync\n"
            "/recent [n] \u2014 Last n tracks, any status (default 10)\n"
            "/track &lt;id&gt; \u2014 Full details for a track\n"
            "/link [id] \u2014 Tappable platform links for a track\n"
            "/card [id] \u2014 Post/refresh a track's status card\n"
            "/logs [n] \u2014 Recent sync log entries\n"
            "\n"
            "<b>Actions</b>\n"
            "/sync \u2014 Force immediate sync (all directions)\n"
            "/sync tg \u2014 Force TG\u2192YT sync only\n"
            "/sync yt \u2014 Force YT\u2192TG sync only\n"
            f"{sp_cmds}"
            "/retry &lt;id&gt; \u2014 Retry a single failed track\n"
            "/retry all \u2014 Retry all failed tracks\n"
            "/retry tg \u2014 Retry all failed TG\u2192YT\n"
            "/retry yt \u2014 Retry all failed YT\u2192TG\n"
            "/delete &lt;id&gt; \u2014 Remove a track from DB\n"
            "\n"
            "<b>Assistant</b>\n"
            "/context \u2014 Conversation context usage\n"
            "/compact \u2014 Summarize + shrink the conversation\n"
            "/reset \u2014 Wipe the conversation memory\n"
            "\n"
            "<b>Debugging</b>\n"
            "/search &lt;query&gt; \u2014 Search YouTube Music\n"
            "/failed [tg|yt|sp] \u2014 List failed tracks\n"
            "/config \u2014 Show current configuration\n"
            "/ping \u2014 Check bot responsiveness\n"
            "/help \u2014 This message"
        )
        await self._reply(update, text)

    # ── /ping ────────────────────────────────────────────────────────

    async def _cmd_ping(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_admin(update):
            return
        uptime = int(time.time() - self._start_time)
        h, m, s = uptime // 3600, (uptime % 3600) // 60, uptime % 60
        await self._reply(update, f"\U0001f3d3 Pong! Uptime: {h}h {m}m {s}s")

    # ── /config ──────────────────────────────────────────────────────

    async def _cmd_config(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_admin(update):
            return
        from navaar.config import Settings
        try:
            s = Settings()
        except Exception:
            await self._reply(update, "\u274c Could not load config.")
            return
        lines = [
            "<b>\u2699\ufe0f Configuration</b>\n",
            f"Channel: <code>{s.telegram_channel_id}</code>",
            f"YT Playlist: <code>{s.ytmusic_playlist_id}</code>",
            f"TG\u2192YT interval: {s.sync_interval_tg_to_yt}s",
            f"YT\u2192TG interval: {s.sync_interval_yt_to_tg}s",
        ]
        if s.spotify_playlist_id:
            lines.extend([
                f"SP Playlist: <code>{s.spotify_playlist_id}</code>",
                f"TG\u2192SP interval: {s.sync_interval_tg_to_sp}s",
                f"SP\u2192TG interval: {s.sync_interval_sp_to_tg}s",
                f"YT\u2192SP interval: {s.sync_interval_yt_to_sp}s",
                f"SP\u2192YT interval: {s.sync_interval_sp_to_yt}s",
            ])
        lines.extend([
            f"Max retries: {s.max_retries}",
            f"Log level: {s.log_level}",
            f"API port: {s.api_port}",
            f"Admins: {list(s.telegram_admin_user_ids)}",
        ])
        await self._reply(update, "\n".join(lines))

    # ── /status ──────────────────────────────────────────────────────

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_admin(update):
            return

        counts = await self._tracks.get_counts()

        uptime = int(time.time() - self._start_time)
        h, m, s = uptime // 3600, (uptime % 3600) // 60, uptime % 60

        lines = [
            "<b>\U0001f4ca Sync Status</b>\n",
            f"\u23f1 Uptime: {h}h {m}m {s}s",
            "",
        ]

        # Determine which directions to show
        active_dirs = {"tg_to_yt": True, "yt_to_tg": True}
        if self._sp_enabled:
            active_dirs.update({
                "tg_to_sp": True, "sp_to_tg": True,
                "yt_to_sp": True, "sp_to_yt": True,
            })

        for direction in active_dirs:
            label = _DIR.get(direction, direction)
            dc = counts.get(direction, {})
            synced = dc.get("synced", 0)
            failed = dc.get("failed", 0)
            pending = dc.get("pending", 0) + dc.get("retry_scheduled", 0)
            dupes = dc.get("duplicate", 0)

            last_ts = await self._state.get(f"last_{direction}_sync") if self._state else None
            last_str = _ago(datetime.fromtimestamp(float(last_ts), tz=UTC)) if last_ts else "never"

            lines.append(f"<b>{label}</b>  (last sync: {last_str})")
            parts = []
            if synced:
                parts.append(f"\u2705 {synced}")
            if pending:
                parts.append(f"\u23f3 {pending}")
            if failed:
                parts.append(f"\u274c {failed}")
            if dupes:
                parts.append(f"\U0001f501 {dupes}")
            lines.append("  " + "  |  ".join(parts) if parts else "  No tracks")
            lines.append("")

        buttons_row1 = [
            InlineKeyboardButton("\U0001f504 Sync TG\u2192YT", callback_data="sync_tg_to_yt"),
            InlineKeyboardButton("\U0001f504 Sync YT\u2192TG", callback_data="sync_yt_to_tg"),
        ]
        buttons_row2 = [
            InlineKeyboardButton("\U0001f4cb Failed", callback_data="show_failed"),
            InlineKeyboardButton("\U0001f4ca Stats", callback_data="show_stats"),
        ]
        rows = [buttons_row1]
        if self._sp_enabled:
            rows.append([
                InlineKeyboardButton("\U0001f504 Sync SP\u2192TG", callback_data="sync_sp_to_tg"),
                InlineKeyboardButton("\U0001f504 Sync SP\u2192YT", callback_data="sync_sp_to_yt"),
            ])
        rows.append(buttons_row2)
        keyboard = InlineKeyboardMarkup(rows)
        await self._reply(update, "\n".join(lines), reply_markup=keyboard)

    # ── /stats ───────────────────────────────────────────────────────

    async def _cmd_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_admin(update):
            return
        stats = await self._tracks.get_stats()
        bar_len = 12
        synced_pct = stats["success_rate"]
        filled = round(bar_len * synced_pct / 100) if stats["total"] else 0
        bar = "\u2588" * filled + "\u2591" * (bar_len - filled)

        synced_detail = (
            f"{stats['tg_to_yt_synced']} TG\u2192YT, {stats['yt_to_tg_synced']} YT\u2192TG"
        )
        if self._sp_enabled:
            synced_detail += (
                f", {stats['tg_to_sp_synced']} TG\u2192SP"
                f", {stats['sp_to_tg_synced']} SP\u2192TG"
                f", {stats['yt_to_sp_synced']} YT\u2192SP"
                f", {stats['sp_to_yt_synced']} SP\u2192YT"
            )

        text = (
            "<b>\U0001f4c8 Statistics</b>\n\n"
            f"Total tracks: <b>{stats['total']}</b>\n"
            f"\u2705 Synced: <b>{stats['synced']}</b>  "
            f"({synced_detail})\n"
            f"\u274c Failed: <b>{stats['failed']}</b>\n"
            f"\U0001f501 Duplicates: <b>{stats['duplicates']}</b>\n"
            f"\u23f3 Pending: <b>{stats['pending']}</b>\n"
            f"\n"
            f"Success rate: <code>[{bar}]</code> {synced_pct}%"
        )
        await self._reply(update, text)

    # ── /queue ───────────────────────────────────────────────────────

    async def _cmd_queue(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_admin(update):
            return
        all_pending = []
        for d in _DIR:
            pending = await self._tracks.get_pending_tracks(d)
            all_pending.extend(pending)

        if not all_pending:
            await self._reply(update, "\u2705 Queue is empty \u2014 nothing pending.")
            return

        lines = [f"<b>\u23f3 Queue ({len(all_pending)} tracks)</b>\n"]
        for t in all_pending[:20]:
            lines.append(_track_line(t, verbose=True))
        if len(all_pending) > 20:
            lines.append(f"\n<i>... and {len(all_pending) - 20} more</i>")
        await self._reply(update, "\n".join(lines))

    # ── /recent [n] ─────────────────────────────────────────────────

    async def _cmd_recent(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_admin(update):
            return
        limit = 10
        if context.args:
            try:
                limit = min(int(context.args[0]), 50)
            except ValueError:
                pass

        tracks = await self._tracks.get_recent_tracks(limit=limit)
        if not tracks:
            await self._reply(update, "No tracks yet.")
            return

        lines = [f"<b>\U0001f55b Recent Tracks (last {len(tracks)})</b>\n"]
        for t in tracks:
            synced_str = _ago(t.synced_at) if t.synced_at else ""
            lines.append(f"{_track_line(t)}  <i>{synced_str}</i>")
        await self._reply(update, "\n".join(lines))

    # ── Logical-track grouping (shared by /health and /digest) ───────

    @staticmethod
    def _logical_key(t) -> tuple[str, str] | None:
        """(origin_prefix, origin_external_id) — the identity of the logical track a
        row belongs to. None when the origin id isn't populated yet."""
        prefix = t.direction.split("_to_")[0]
        field = {"tg": "tg_file_id", "yt": "yt_video_id", "sp": "sp_track_id"}.get(prefix)
        value = getattr(t, field) if field else None
        return (prefix, value) if value else None

    def _group_logical(self, tracks: list) -> dict:
        """Group rows into logical tracks keyed by origin. Value: {prefix, rows}."""
        groups: dict[tuple[str, str], dict] = {}
        for t in tracks:
            key = self._logical_key(t)
            if key is None:
                continue
            g = groups.setdefault(key, {"prefix": key[0], "rows": []})
            g["rows"].append(t)
        return groups

    # ── /health ──────────────────────────────────────────────────────

    async def _cmd_health(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Report logical tracks that landed on some services but not others — the
        mesh's characteristic partial-sync gap (e.g. on YouTube Music but failed on
        Spotify), which aggregate /stats and per-direction /failed both hide."""
        if not self._is_admin(update):
            return
        all_tracks = await self._tracks.get_all_tracks()
        groups = self._group_logical(all_tracks)

        partials = []
        for _key, g in groups.items():
            failed = [r for r in g["rows"] if r.status == "failed"]
            if failed:
                partials.append((g, failed))

        if not partials:
            await self._reply(
                update,
                "✅ <b>Health:</b> every tracked song is synced across its platforms "
                "(no partial-sync gaps).",
            )
            return

        # Newest logical track first (by max row id in the group).
        partials.sort(key=lambda p: max(r.id for r in p[0]["rows"]), reverse=True)
        lines = [f"<b>\U0001fa7a Health — {len(partials)} partial track(s)</b>", ""]
        buttons = []
        for g, failed in partials[:15]:
            rows = g["rows"]
            primary = min(rows, key=lambda r: r.id)
            artist = html.escape(primary.artist or "Unknown")
            title = html.escape(primary.title or "")
            src = SVC_LABEL.get(g["prefix"], g["prefix"])
            miss = ", ".join(
                f"{SVC_LABEL.get(r.direction.split('_to_')[1], r.direction)}"
                for r in failed
            )
            lines.append(f"❌ <code>#{primary.id}</code> {artist} — {title}")
            lines.append(f"   from {src} · missing on <b>{miss}</b>")
            # One retry button per failed target (not just the first), so a track
            # missing on two platforms can be fully re-queued in one pass.
            buttons.append([
                InlineKeyboardButton(
                    f"\U0001f504 #{primary.id} {SVC_LABEL.get(r.direction.split('_to_')[1], r.direction)}",
                    callback_data=f"retry_{r.id}",
                )
                for r in failed
            ])
        if len(partials) > 15:
            lines.append(f"\n<i>... and {len(partials) - 15} more</i>")
        keyboard = InlineKeyboardMarkup(buttons) if buttons else None
        await self._reply(update, "\n".join(lines), reply_markup=keyboard)

    # ── /digest [days] ───────────────────────────────────────────────

    async def _cmd_digest(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """A recap of what the group added recently (default: last 7 days), one line
        per logical track — turns the shared playlist into a weekly ritual."""
        if not self._is_admin(update):
            return
        days = 7
        if context.args:
            try:
                days = max(1, min(int(context.args[0]), 90))
            except ValueError:
                pass
        since = datetime.now(UTC) - timedelta(days=days)
        tracks = await self._tracks.get_tracks_since(since)
        groups = self._group_logical(tracks)
        if not groups:
            await self._reply(update, f"\U0001f4ed Nothing added in the last {days} day(s).")
            return

        # Newest logical track first.
        ordered = sorted(
            groups.values(), key=lambda g: max(r.id for r in g["rows"]), reverse=True
        )
        lines = [
            f"<b>\U0001f4c5 Digest — {len(ordered)} track(s) in the last {days} day(s)</b>",
            "",
        ]
        for g in ordered[:30]:
            rows = g["rows"]
            primary = min(rows, key=lambda r: r.id)
            artist = html.escape(primary.artist or "Unknown")
            title = html.escape(primary.title or "")
            icon = SVC_ICON.get(g["prefix"], "\U0001f3b5")
            # A compact per-service status footprint.
            marks = []
            for r in sorted(rows, key=lambda r: r.id):
                tgt = r.direction.split("_to_")[1]
                marks.append(f"{SVC_ICON.get(tgt, tgt)}{_S.get(r.status, '')}")
            lines.append(f"{icon} {artist} — {title}  {' '.join(marks)}")
        if len(ordered) > 30:
            lines.append(f"\n<i>... and {len(ordered) - 30} more</i>")
        await self._reply(update, "\n".join(lines))

    # ── /link [id] ───────────────────────────────────────────────────

    async def _cmd_link(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Reply with tappable YouTube Music / Spotify / Telegram links for a track
        (defaults to the most recent) — a lightweight, shareable alternative to the
        verbose /track dump, usable in a DM."""
        if not self._is_admin(update):
            return
        if context.args:
            try:
                track_id = int(context.args[0].lstrip("#"))
            except ValueError:
                await self._reply(update, "❌ Invalid track ID.")
                return
            t = await self._tracks.get_track(track_id)
        else:
            recent = await self._tracks.get_recent_tracks(limit=1)
            t = recent[0] if recent else None
        if not t:
            await self._reply(update, "❌ No track found.")
            return

        siblings = await self._tracks.get_sibling_tracks(t)
        yt = next((s.yt_video_id for s in siblings if s.yt_video_id), None)
        sp = next((s.sp_track_id for s in siblings if s.sp_track_id), None)
        tg_msg = min((s.tg_message_id for s in siblings if s.tg_message_id), default=None)

        buttons = []
        if yt:
            buttons.append(InlineKeyboardButton(
                "▶️ YouTube Music", url=f"https://music.youtube.com/watch?v={yt}"))
        if sp:
            buttons.append(InlineKeyboardButton(
                "\U0001f7e2 Spotify", url=f"https://open.spotify.com/track/{sp}"))
        tme = self._tme_link(tg_msg) if tg_msg else None
        if tme:
            buttons.append(InlineKeyboardButton("\U0001f4e8 Telegram", url=tme))

        artist = html.escape(t.artist or "Unknown")
        title = html.escape(t.title or "")
        header = f"\U0001f517 <b>{artist} — {title}</b> (#{t.id})"
        if not buttons:
            await self._reply(update, f"{header}\n<i>No platform links yet.</i>")
            return
        await self._reply(update, header, reply_markup=InlineKeyboardMarkup([buttons]))

    def _tme_link(self, message_id: int) -> str | None:
        cid = str(self._channel_id)
        if cid.startswith("-100"):
            return f"https://t.me/c/{cid[4:]}/{message_id}"
        return None

    # ── /track <id> ──────────────────────────────────────────────────

    async def _cmd_track(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_admin(update):
            return
        if not context.args:
            await self._reply(update, "Usage: /track &lt;id&gt;")
            return
        try:
            track_id = int(context.args[0].lstrip("#"))
        except ValueError:
            await self._reply(update, "\u274c Invalid track ID.")
            return

        t = await self._tracks.get_track(track_id)
        if not t:
            await self._reply(update, f"\u274c Track #{track_id} not found.")
            return

        icon = _S.get(t.status, "\u2753")
        artist = html.escape(t.artist or "Unknown")
        title = html.escape(t.title)

        lines = [
            f"<b>{icon} Track #{t.id}</b>\n",
            f"<b>Title:</b> {title}",
            f"<b>Artist:</b> {artist}",
            f"<b>Direction:</b> {_DIR.get(t.direction, t.direction)}",
            f"<b>Status:</b> {t.status}",
            f"<b>Method:</b> {t.identification_method or 'n/a'}",
            "",
        ]
        if t.yt_video_id:
            lines.append(f"<b>YT Video:</b> <code>{t.yt_video_id}</code>")
            lines.append(f"<b>YT Link:</b> https://music.youtube.com/watch?v={t.yt_video_id}")
        if t.sp_track_id:
            lines.append(f"<b>SP Track:</b> <code>{t.sp_track_id}</code>")
            lines.append(f"<b>SP Link:</b> https://open.spotify.com/track/{t.sp_track_id}")
        if t.tg_message_id:
            lines.append(f"<b>TG Message:</b> {t.tg_message_id}")
        if t.tg_file_unique_id:
            lines.append(f"<b>TG File:</b> <code>{t.tg_file_unique_id}</code>")
        if t.duration_seconds:
            m, s = divmod(t.duration_seconds, 60)
            lines.append(f"<b>Duration:</b> {m}:{s:02d}")

        lines.append("")
        if t.failure_reason:
            lines.append(f"\u274c <b>Failure:</b> <i>{html.escape(t.failure_reason)}</i>")
        lines.append(f"<b>Retries:</b> {t.retry_count}/{t.max_retries}")
        lines.append(f"<b>Created:</b> {_ago(t.created_at)}")
        if t.synced_at:
            lines.append(f"<b>Synced:</b> {_ago(t.synced_at)}")

        # Log history
        logs = await self._log.get_logs_for_track(t.id, limit=5)
        if logs:
            lines.append("\n<b>Log:</b>")
            for entry in reversed(logs):
                lines.append(f"  \u2022 {entry.event} ({_ago(entry.created_at)})")

        buttons = []
        if t.status == "failed":
            buttons.append(InlineKeyboardButton(
                "\U0001f504 Retry", callback_data=f"retry_{t.id}"
            ))
        buttons.append(InlineKeyboardButton(
            "\U0001f5d1 Delete", callback_data=f"delete_{t.id}"
        ))
        keyboard = InlineKeyboardMarkup([buttons])

        await self._reply(update, "\n".join(lines), reply_markup=keyboard)

    # ── /card [id] ───────────────────────────────────────────────────

    async def _cmd_card(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_admin(update):
            return
        if self._card is None:
            await self._reply(update, "❌ Status cards are disabled.")
            return

        if context.args:
            try:
                track_id = int(context.args[0].lstrip("#"))
            except ValueError:
                await self._reply(update, "❌ Invalid track ID.")
                return
            t = await self._tracks.get_track(track_id)
        else:
            recent = await self._tracks.get_recent_tracks(limit=1)
            t = recent[0] if recent else None

        if not t:
            await self._reply(update, "❌ No track to build a card for.")
            return

        await self._card.refresh(t.id)
        await self._reply(
            update, f"\U0001f4c7 Status card posted/refreshed for track <code>#{t.id}</code>."
        )

    # ── /logs [n] ────────────────────────────────────────────────────

    async def _cmd_logs(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_admin(update):
            return
        limit = 15
        if context.args:
            try:
                limit = min(int(context.args[0]), 50)
            except ValueError:
                pass

        logs = await self._log.get_recent_logs(limit=limit)
        if not logs:
            await self._reply(update, "No log entries yet.")
            return

        lines = [f"<b>\U0001f4dc Recent Logs (last {len(logs)})</b>\n"]
        for entry in logs:
            tid = f"#{entry.track_id}" if entry.track_id else "-"
            direction = _DIR.get(entry.direction, "") if entry.direction else ""
            lines.append(f"<code>{tid:>5}</code> {entry.event} {direction} <i>{_ago(entry.created_at)}</i>")
        await self._reply(update, "\n".join(lines))

    # ── /failed [tg|yt|sp] ──────────────────────────────────────────

    async def _cmd_failed(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_admin(update):
            return
        direction = None
        if context.args:
            arg = context.args[0].lower()
            _dir_map = {
                "tg": "tg_to_yt", "tg_to_yt": "tg_to_yt",
                "yt": "yt_to_tg", "yt_to_tg": "yt_to_tg",
                "sp": None,  # will get all SP directions
                "tg_to_sp": "tg_to_sp", "sp_to_tg": "sp_to_tg",
                "yt_to_sp": "yt_to_sp", "sp_to_yt": "sp_to_yt",
            }
            if arg == "sp":
                # Show all Spotify-related failures
                failed = []
                for d in ("tg_to_sp", "sp_to_tg", "yt_to_sp", "sp_to_yt"):
                    failed.extend(await self._tracks.get_failed_tracks(d))
            else:
                direction = _dir_map.get(arg)
                failed = await self._tracks.get_failed_tracks(direction)
        else:
            failed = await self._tracks.get_failed_tracks(direction)

        if not failed:
            await self._reply(update, "\u2705 No failed tracks!")
            return

        lines = [f"<b>\u274c Failed Tracks ({len(failed)})</b>\n"]
        for t in failed[:20]:
            lines.append(_track_line(t, verbose=True))
        if len(failed) > 20:
            lines.append(f"\n<i>... and {len(failed) - 20} more</i>")

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("\U0001f504 Retry All", callback_data="retry_all")],
        ])
        await self._reply(update, "\n".join(lines), reply_markup=keyboard)

    # ── /sync [tg|yt|sp|all] ────────────────────────────────────────

    async def _cmd_sync(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_admin(update):
            return
        if not self._engine:
            await self._reply(update, "\u274c Sync engine not available.")
            return

        arg = context.args[0].lower() if context.args else "all"
        directions = []
        if arg in ("tg", "tg_to_yt"):
            directions = ["tg_to_yt"]
        elif arg in ("yt", "yt_to_tg"):
            directions = ["yt_to_tg"]
        elif arg == "sp":
            directions = ["sp_to_tg", "sp_to_yt"]
        elif arg == "all":
            directions = ["tg_to_yt", "yt_to_tg"]
            if self._sp_enabled:
                directions.extend(["tg_to_sp", "sp_to_tg", "yt_to_sp", "sp_to_yt"])
        else:
            # Try exact direction name
            if arg in _DIR:
                directions = [arg]
            else:
                directions = ["tg_to_yt", "yt_to_tg"]

        for d in directions:
            self._engine.force_sync(d)

        labels = [_DIR.get(d, d) for d in directions]
        await self._reply(update, f"\U0001f504 Sync triggered: {', '.join(labels)}")

    async def _reset_all_failed_metered(self) -> int:
        """Reset every failed track for retry, incrementing RETRIES_TOTAL by each
        direction's OWN count. Resetting globally and then adding the global total to
        all six direction counters (the old code) over-counted the metric 6x and
        attributed retries to directions that had none. Returns the true total."""
        total = 0
        for d in _DIR:
            count = await self._tracks.reset_all_failed(d)
            if count:
                RETRIES_TOTAL.labels(direction=d).inc(count)
            total += count
        return total

    # ── /retry <id|all|tg|yt|sp> ─────────────────────────────────────

    async def _cmd_retry(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_admin(update):
            return
        if not context.args:
            await self._reply(update, "Usage: /retry &lt;id|all|tg|yt|sp&gt;")
            return

        arg = context.args[0].lower()
        if arg == "all":
            count = await self._reset_all_failed_metered()
            await self._reply(update, f"\U0001f504 Reset {count} failed tracks for retry.")
        elif arg in ("tg", "tg_to_yt"):
            count = await self._tracks.reset_all_failed("tg_to_yt")
            RETRIES_TOTAL.labels(direction="tg_to_yt").inc(count)
            await self._reply(update, f"\U0001f504 Reset {count} failed TG\u2192YT tracks.")
        elif arg in ("yt", "yt_to_tg"):
            count = await self._tracks.reset_all_failed("yt_to_tg")
            RETRIES_TOTAL.labels(direction="yt_to_tg").inc(count)
            await self._reply(update, f"\U0001f504 Reset {count} failed YT\u2192TG tracks.")
        elif arg == "sp":
            total = 0
            for d in ("tg_to_sp", "sp_to_tg", "yt_to_sp", "sp_to_yt"):
                count = await self._tracks.reset_all_failed(d)
                RETRIES_TOTAL.labels(direction=d).inc(count)
                total += count
            await self._reply(update, f"\U0001f504 Reset {total} failed Spotify tracks.")
        else:
            try:
                track_id = int(arg.lstrip("#"))
            except ValueError:
                await self._reply(update, "\u274c Invalid. Use: /retry &lt;id|all|tg|yt|sp&gt;")
                return
            track = await self._tracks.get_track(track_id)
            if not track:
                await self._reply(update, f"\u274c Track #{track_id} not found.")
                return
            if track.status != "failed":
                await self._reply(
                    update,
                    f"\u274c Track #{track_id} is <b>{track.status}</b>, not failed.",
                )
                return
            await self._tracks.reset_for_retry(track_id)
            RETRIES_TOTAL.labels(direction=track.direction).inc()
            await self._reply(update, f"\U0001f504 Track #{track_id} queued for retry.")

    # ── /delete <id> ─────────────────────────────────────────────────

    async def _cmd_delete(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_admin(update):
            return
        if not context.args:
            await self._reply(update, "Usage: /delete &lt;id&gt;")
            return
        try:
            track_id = int(context.args[0].lstrip("#"))
        except ValueError:
            await self._reply(update, "\u274c Invalid track ID.")
            return

        summary = await self._purge_logical_track(track_id)
        await self._reply(update, summary)

    async def _purge_logical_track(self, track_id: int) -> str:
        """Fully remove a *logical* track: take it off both playlists, delete its
        channel audio message(s) and status card, and delete every sibling DB row \u2014
        matching the documented 'delete' semantics. Previously /delete removed only a
        single DB row, leaving the song live on both playlists and in the channel
        while replying 'deleted' (a false success). Each external step is best-effort
        so one failure can't block the rest; returns an HTML summary of what happened."""
        track = await self._tracks.get_track(track_id)
        if not track:
            return f"\u274c Track #{track_id} not found."
        siblings = await self._tracks.get_sibling_tracks(track)

        yt_ids = {s.yt_video_id for s in siblings if s.yt_video_id}
        sp_ids = {s.sp_track_id for s in siblings if s.sp_track_id}
        msg_ids = {
            m
            for s in siblings
            for m in (s.tg_message_id, s.card_message_id)
            if m
        }

        removed: list[str] = []
        for vid in yt_ids:
            if self._yt is not None and await self._safe_remove(self._yt, vid, "yt"):
                removed.append("YT")
        for sid in sp_ids:
            if self._sp is not None and await self._safe_remove(self._sp, sid, "sp"):
                removed.append("SP")

        deleted_msgs = 0
        for mid in msg_ids:
            if await self._safe_delete_message(mid):
                deleted_msgs += 1

        rows = await self._tracks.delete_tracks([s.id for s in siblings])

        artist = html.escape(track.artist or "Unknown")
        title = html.escape(track.title or "")
        parts = [f"\U0001f5d1 Deleted <b>{artist} \u2014 {title}</b> (#{track_id})"]
        detail = []
        if removed:
            detail.append("removed from " + "+".join(sorted(set(removed))))
        if deleted_msgs:
            detail.append(f"{deleted_msgs} channel message(s) deleted")
        detail.append(f"{rows} DB row(s) removed")
        parts.append("\u2022 " + "; ".join(detail))
        return "\n".join(parts)

    async def _safe_remove(self, client: object, ext_id: str, svc: str) -> bool:
        """Remove one external id from a playlist off the event loop; swallow errors.
        Reflects the real outcome so the /delete summary doesn't overstate: the YT
        client returns False when the id wasn't actually in the playlist (don't claim
        removal then); the SP client returns None (no signal) — treat a clean call as
        done."""
        try:
            result = await asyncio.to_thread(client.remove_from_playlist, ext_id)
        except Exception:
            logger.warning("purge_playlist_remove_failed", service=svc, ext_id=ext_id, exc_info=True)
            return False
        return True if result is None else bool(result)

    async def _safe_delete_message(self, message_id: int) -> bool:
        if self._app is None:
            return False
        try:
            await self._app.bot.delete_message(chat_id=self._channel_id, message_id=message_id)
            return True
        except Exception:
            logger.debug("purge_delete_message_failed", message_id=message_id, exc_info=True)
            return False

    # ── /search <query> ──────────────────────────────────────────────

    async def _cmd_search(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_admin(update):
            return
        if not context.args:
            await self._reply(update, "Usage: /search &lt;query&gt;")
            return
        if not self._yt:
            await self._reply(update, "\u274c YT Music client not available.")
            return

        query = " ".join(context.args)
        await self._reply(update, f"\U0001f50d Searching: <i>{html.escape(query)}</i>...")

        try:
            results = self._yt.search_song(query, limit=5)
        except Exception as e:
            await self._reply(update, f"\u274c Search failed: {html.escape(str(e)[:100])}")
            return

        if not results:
            await self._reply(update, "No results found.")
            return

        lines = [f"<b>\U0001f3b5 Results for: {html.escape(query)}</b>\n"]
        buttons = []
        for i, r in enumerate(results, 1):
            artists = ", ".join(a["name"] for a in r.get("artists", []))
            vid = r.get("videoId", "?")
            title = r.get("title", "?")
            lines.append(
                f"{i}. {html.escape(artists)} \u2014 {html.escape(title)}\n"
                f"   <code>{vid}</code>"
            )
            if vid and vid != "?":
                buttons.append([
                    InlineKeyboardButton(f"\u2795 Add #{i}: {title[:30]}", callback_data=f"add_yt_{vid}")
                ])
        keyboard = InlineKeyboardMarkup(buttons) if buttons else None
        await self._reply(update, "\n".join(lines), reply_markup=keyboard)

    # ── /search_sp <query> ───────────────────────────────────────────

    async def _cmd_search_sp(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_admin(update):
            return
        if not context.args:
            await self._reply(update, "Usage: /search_sp &lt;query&gt;")
            return
        if not self._sp:
            await self._reply(update, "\u274c Spotify client not available.")
            return

        query = " ".join(context.args)
        await self._reply(update, f"\U0001f50d Searching Spotify: <i>{html.escape(query)}</i>...")

        try:
            results = self._sp.search_track(query, limit=5)
        except Exception as e:
            await self._reply(update, f"\u274c Search failed: {html.escape(str(e)[:100])}")
            return

        if not results:
            await self._reply(update, "No results found.")
            return

        lines = [f"<b>\U0001f3b6 Spotify Results for: {html.escape(query)}</b>\n"]
        buttons = []
        for i, r in enumerate(results, 1):
            artists = ", ".join(r.get("artists", []))
            tid = r.get("id", "?")
            name = r.get("name", "?")
            lines.append(
                f"{i}. {html.escape(artists)} \u2014 {html.escape(name)}\n"
                f"   <code>{tid}</code>"
            )
            if tid and tid != "?":
                buttons.append([
                    InlineKeyboardButton(f"\u2795 Add #{i}: {name[:30]}", callback_data=f"add_sp_{tid}")
                ])
        keyboard = InlineKeyboardMarkup(buttons) if buttons else None
        await self._reply(update, "\n".join(lines), reply_markup=keyboard)

    # \u2500\u2500 Search-and-add: add a chosen result to the shared playlists \u2500\u2500

    async def _handle_add_callback(self, query, data: str) -> None:
        """Add a track chosen from a /search result to the shared playlists. We add
        it to the source playlist (YT or SP) and let the existing pull loop ingest +
        fan it out to the other services \u2014 reusing all the dedup/fan-out machinery \u2014
        then force that loop to run now. Admin-gated by the caller."""
        try:
            _, svc, ext_id = data.split("_", 2)
        except ValueError:
            return
        if svc == "yt" and self._yt is not None:
            try:
                await asyncio.to_thread(self._yt.add_to_playlist, ext_id)
            except Exception:
                logger.warning("add_yt_failed", ext_id=ext_id, exc_info=True)
                await query.message.reply_text("\u274c Couldn't add to YouTube Music.")
                return
            if self._engine:
                self._engine.force_sync("yt_to_tg")
            await query.message.reply_text(
                "\u2705 Added to YouTube Music \u2014 it'll sync to the channel"
                + (" and Spotify" if self._sp_enabled else "") + " shortly.",
            )
        elif svc == "sp" and self._sp is not None:
            try:
                await asyncio.to_thread(self._sp.add_to_playlist, ext_id)
            except Exception:
                logger.warning("add_sp_failed", ext_id=ext_id, exc_info=True)
                await query.message.reply_text("\u274c Couldn't add to Spotify.")
                return
            if self._engine:
                self._engine.force_sync("sp_to_tg")
            await query.message.reply_text(
                "\u2705 Added to Spotify \u2014 it'll sync to the channel and YouTube Music shortly.",
            )
        else:
            await query.message.reply_text("\u274c That service isn't available.")

    # ── Inline button callbacks ──────────────────────────────────────

    async def _handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not query or not query.from_user or query.from_user.id not in self._admin_ids:
            await query.answer("\u274c Unauthorized")
            return

        data = query.data
        await query.answer()

        if data in ("sync_tg_to_yt", "sync_yt_to_tg", "sync_sp_to_tg", "sync_sp_to_yt"):
            if self._engine:
                self._engine.force_sync(data.removeprefix("sync_"))
                label = _DIR.get(data.removeprefix("sync_"), data)
                await query.message.reply_text(
                    f"\U0001f504 {label} sync triggered!", parse_mode="HTML"
                )
        elif data == "show_failed":
            await self._cmd_failed(update, context)
        elif data == "show_stats":
            await self._cmd_stats(update, context)
        elif data == "retry_all":
            count = await self._reset_all_failed_metered()
            await query.message.reply_text(
                f"\U0001f504 Reset {count} failed tracks for retry.", parse_mode="HTML"
            )
        elif data.startswith("retry_"):
            track_id = int(data.split("_")[1])
            track = await self._tracks.get_track(track_id)
            if track and track.status == "failed":
                await self._tracks.reset_for_retry(track_id)
                RETRIES_TOTAL.labels(direction=track.direction).inc()
                if self._engine:
                    self._engine.force_sync(track.direction)
                if self._card:
                    await self._card.refresh(track_id)
                await query.message.reply_text(
                    f"\U0001f504 Track #{track_id} queued for retry.", parse_mode="HTML"
                )
            else:
                await query.message.reply_text(
                    f"\u274c Track #{track_id} is not in failed state.", parse_mode="HTML"
                )
        elif data.startswith("delete_"):
            track_id = int(data.split("_")[1])
            summary = await self._purge_logical_track(track_id)
            await query.message.reply_text(summary, parse_mode="HTML")
        elif data.startswith("add_"):
            await self._handle_add_callback(query, data)
        else:
            logger.warning("unknown_callback", data=data)

    # ── Slash-command menu ───────────────────────────────────────────

    def _menu_commands(self) -> list[BotCommand]:
        """The slash-command list shown in Telegram's `/` menu. Mirrors the
        handlers registered in ``build_app`` (aliases omitted); ``search_sp``
        only appears when Spotify is enabled."""
        commands = [
            BotCommand("status", "Live sync status dashboard"),
            BotCommand("stats", "Aggregate statistics"),
            BotCommand("queue", "Pending tracks waiting to sync"),
            BotCommand("recent", "Last N tracks, any status: /recent [n]"),
            BotCommand("track", "Full details for a track: /track <id>"),
            BotCommand("link", "Tappable platform links: /link [id]"),
            BotCommand("health", "Tracks missing on some platforms"),
            BotCommand("digest", "Recap of recent additions: /digest [days]"),
            BotCommand("card", "Post/refresh a track's status card: /card [id]"),
            BotCommand("logs", "Recent sync log entries: /logs [n]"),
            BotCommand("sync", "Force sync: /sync [tg|yt|sp|all]"),
            BotCommand("retry", "Retry failed: /retry <id|all|tg|yt|sp>"),
            BotCommand("delete", "Remove a track from the DB: /delete <id>"),
            BotCommand("failed", "List failed tracks: /failed [tg|yt|sp]"),
            BotCommand("search", "Search YouTube Music: /search <query>"),
        ]
        if self._sp_enabled:
            commands.append(BotCommand("search_sp", "Search Spotify: /search_sp <query>"))
        if self._agent is not None:
            commands.extend([
                BotCommand("context", "Show the agent's conversation context usage"),
                BotCommand("compact", "Summarize + shrink the agent's conversation"),
                BotCommand("reset", "Wipe the agent's conversation memory"),
            ])
        commands.extend([
            BotCommand("config", "Show current configuration"),
            BotCommand("ping", "Check bot responsiveness"),
            BotCommand("help", "Show all commands"),
        ])
        return commands

    async def set_command_menu(self) -> None:
        """Register the `/` autocomplete menu via Telegram's setMyCommands.

        Scoped per-admin chat so the commands surface only for admins (every
        handler is admin-gated anyway). Best-effort: an admin who never opened a
        chat with the bot is skipped, and any API error is logged but never
        blocks startup."""
        if self._app is None:
            return
        try:
            me = await self._app.bot.get_me()
            self._bot_username = me.username
        except Exception:
            logger.warning("get_me_failed", exc_info=True)
        commands = self._menu_commands()
        registered = 0
        for admin_id in self._admin_ids:
            try:
                await self._app.bot.set_my_commands(
                    commands, scope=BotCommandScopeChat(chat_id=admin_id)
                )
                registered += 1
            except Exception:
                logger.warning("set_my_commands_failed", admin_id=admin_id, exc_info=True)
        logger.info("command_menu_registered", commands=len(commands), admins=registered)

    # ── Build application ────────────────────────────────────────────

    def build_app(self) -> Application:
        self._app = (
            Application.builder()
            .token(self._token)
            .read_timeout(120)
            .write_timeout(120)
            .connect_timeout(30)
            .build()
        )

        # Channel post handler
        self._app.add_handler(
            MessageHandler(filters.AUDIO & filters.UpdateType.CHANNEL_POST, self._handle_channel_post)
        )

        # Natural-language control: text post in the channel (reply + @mention).
        self._app.add_handler(
            MessageHandler(
                filters.TEXT & filters.UpdateType.CHANNEL_POST, self._handle_channel_command
            )
        )

        # Inline button callback handler
        self._app.add_handler(CallbackQueryHandler(self._handle_callback))

        # Admin commands
        commands = {
            "start": self._cmd_start,
            "help": self._cmd_help,
            "ping": self._cmd_ping,
            "config": self._cmd_config,
            "status": self._cmd_status,
            "stats": self._cmd_stats,
            "queue": self._cmd_queue,
            "recent": self._cmd_recent,
            "track": self._cmd_track,
            "link": self._cmd_link,
            "health": self._cmd_health,
            "digest": self._cmd_digest,
            "card": self._cmd_card,
            "logs": self._cmd_logs,
            "failed": self._cmd_failed,
            "list_failed": self._cmd_failed,  # alias
            "sync": self._cmd_sync,
            "force_sync": self._cmd_sync,  # alias
            "retry": self._cmd_retry,
            "delete": self._cmd_delete,
            "search": self._cmd_search,
            "search_sp": self._cmd_search_sp,
            "reset": self._cmd_reset,
            "context": self._cmd_context,
            "compact": self._cmd_compact,
        }
        for name, handler in commands.items():
            self._app.add_handler(CommandHandler(name, handler))

        # Natural-language control via admin DM (non-command text). Added after the
        # command handlers so slash commands take precedence.
        self._app.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
                self._handle_dm_message,
            )
        )

        return self._app
