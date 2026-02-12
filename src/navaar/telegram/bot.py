from __future__ import annotations

import html
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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

if TYPE_CHECKING:
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
}

_DIR = {
    "tg_to_yt": "\U0001f4e4 TG \u2192 YT",
    "yt_to_tg": "\U0001f4e5 YT \u2192 TG",
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
        if t.failure_reason:
            line += f"\n   Reason: <i>{html.escape(t.failure_reason[:80])}</i>"
    return line


def _ago(dt: datetime | None) -> str:
    if not dt:
        return "never"
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
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
    ) -> None:
        self._token = token
        self._channel_id = channel_id
        self._admin_ids = set(admin_user_ids)
        self._tracks = track_repo
        self._log = sync_log
        self._state = sync_state
        self._engine = sync_engine
        self._yt = yt_client
        self._app: Application | None = None
        self._start_time = time.time()

    def set_sync_engine(self, engine: SyncEngine) -> None:
        self._engine = engine

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

        # Ignore messages sent by the bot itself (YT->TG uploads)
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
        track = await self._tracks.create_track(
            direction="tg_to_yt",
            status="pending",
            title=title,
            artist=audio.performer,
            tg_message_id=message.message_id,
            tg_file_id=audio.file_id,
            tg_file_unique_id=audio.file_unique_id,
            duration_seconds=audio.duration,
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

    # ── /start, /help ────────────────────────────────────────────────

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_admin(update):
            return
        await self._cmd_help(update, context)

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_admin(update):
            return
        text = (
            "<b>\U0001f3b5 Navaar \u2014 Bot Commands</b>\n"
            "\n"
            "<b>Monitoring</b>\n"
            "/status \u2014 Live sync status dashboard\n"
            "/stats \u2014 Aggregate statistics\n"
            "/queue \u2014 Pending tracks waiting to sync\n"
            "/recent [n] \u2014 Last n synced tracks (default 10)\n"
            "/track &lt;id&gt; \u2014 Full details for a track\n"
            "/logs [n] \u2014 Recent sync log entries\n"
            "\n"
            "<b>Actions</b>\n"
            "/sync \u2014 Force immediate sync (both directions)\n"
            "/sync tg \u2014 Force TG\u2192YT sync only\n"
            "/sync yt \u2014 Force YT\u2192TG sync only\n"
            "/retry &lt;id&gt; \u2014 Retry a single failed track\n"
            "/retry all \u2014 Retry all failed tracks\n"
            "/retry tg \u2014 Retry all failed TG\u2192YT\n"
            "/retry yt \u2014 Retry all failed YT\u2192TG\n"
            "/delete &lt;id&gt; \u2014 Remove a track from DB\n"
            "\n"
            "<b>Debugging</b>\n"
            "/search &lt;query&gt; \u2014 Search YouTube Music\n"
            "/failed [tg|yt] \u2014 List failed tracks\n"
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
        text = (
            "<b>\u2699\ufe0f Configuration</b>\n\n"
            f"Channel: <code>{s.telegram_channel_id}</code>\n"
            f"Playlist: <code>{s.ytmusic_playlist_id}</code>\n"
            f"TG\u2192YT interval: {s.sync_interval_tg_to_yt}s\n"
            f"YT\u2192TG interval: {s.sync_interval_yt_to_tg}s\n"
            f"Max retries: {s.max_retries}\n"
            f"Log level: {s.log_level}\n"
            f"API port: {s.api_port}\n"
            f"Admins: {list(s.telegram_admin_user_ids)}"
        )
        await self._reply(update, text)

    # ── /status ──────────────────────────────────────────────────────

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_admin(update):
            return

        counts = await self._tracks.get_counts()
        last_tg = await self._state.get("last_tg_to_yt_sync") if self._state else None
        last_yt = await self._state.get("last_yt_to_tg_sync") if self._state else None

        uptime = int(time.time() - self._start_time)
        h, m, s = uptime // 3600, (uptime % 3600) // 60, uptime % 60

        lines = [
            "<b>\U0001f4ca Sync Status</b>\n",
            f"\u23f1 Uptime: {h}h {m}m {s}s",
            "",
        ]

        for direction, label in _DIR.items():
            dc = counts.get(direction, {})
            total_dir = sum(dc.values())
            synced = dc.get("synced", 0)
            failed = dc.get("failed", 0)
            pending = dc.get("pending", 0) + dc.get("retry_scheduled", 0)
            dupes = dc.get("duplicate", 0)

            last_ts = last_tg if direction == "tg_to_yt" else last_yt
            last_str = _ago(datetime.fromtimestamp(float(last_ts), tz=timezone.utc)) if last_ts else "never"

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

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("\U0001f504 Sync TG\u2192YT", callback_data="sync_tg_to_yt"),
                InlineKeyboardButton("\U0001f504 Sync YT\u2192TG", callback_data="sync_yt_to_tg"),
            ],
            [
                InlineKeyboardButton("\U0001f4cb Failed", callback_data="show_failed"),
                InlineKeyboardButton("\U0001f4ca Stats", callback_data="show_stats"),
            ],
        ])
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

        text = (
            "<b>\U0001f4c8 Statistics</b>\n\n"
            f"Total tracks: <b>{stats['total']}</b>\n"
            f"\u2705 Synced: <b>{stats['synced']}</b>  "
            f"({stats['tg_to_yt_synced']} TG\u2192YT, {stats['yt_to_tg_synced']} YT\u2192TG)\n"
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
        pending_tg = await self._tracks.get_pending_tracks("tg_to_yt")
        pending_yt = await self._tracks.get_pending_tracks("yt_to_tg")
        all_pending = pending_tg + pending_yt

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

    # ── /failed [tg|yt] ─────────────────────────────────────────────

    async def _cmd_failed(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_admin(update):
            return
        direction = None
        if context.args:
            arg = context.args[0].lower()
            if arg in ("tg", "tg_to_yt"):
                direction = "tg_to_yt"
            elif arg in ("yt", "yt_to_tg"):
                direction = "yt_to_tg"

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

    # ── /sync [tg|yt] ───────────────────────────────────────────────

    async def _cmd_sync(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_admin(update):
            return
        if not self._engine:
            await self._reply(update, "\u274c Sync engine not available.")
            return

        arg = context.args[0].lower() if context.args else "both"
        directions = []
        if arg in ("tg", "tg_to_yt"):
            directions = ["tg_to_yt"]
        elif arg in ("yt", "yt_to_tg"):
            directions = ["yt_to_tg"]
        else:
            directions = ["tg_to_yt", "yt_to_tg"]

        for d in directions:
            self._engine.force_sync(d)

        labels = [_DIR[d] for d in directions]
        await self._reply(update, f"\U0001f504 Sync triggered: {', '.join(labels)}")

    # ── /retry <id|all|tg|yt> ────────────────────────────────────────

    async def _cmd_retry(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_admin(update):
            return
        if not context.args:
            await self._reply(update, "Usage: /retry &lt;id|all|tg|yt&gt;")
            return

        arg = context.args[0].lower()
        if arg == "all":
            count = await self._tracks.reset_all_failed()
            for d in ("tg_to_yt", "yt_to_tg"):
                RETRIES_TOTAL.labels(direction=d).inc(count)
            await self._reply(update, f"\U0001f504 Reset {count} failed tracks for retry.")
        elif arg in ("tg", "tg_to_yt"):
            count = await self._tracks.reset_all_failed("tg_to_yt")
            RETRIES_TOTAL.labels(direction="tg_to_yt").inc(count)
            await self._reply(update, f"\U0001f504 Reset {count} failed TG\u2192YT tracks.")
        elif arg in ("yt", "yt_to_tg"):
            count = await self._tracks.reset_all_failed("yt_to_tg")
            RETRIES_TOTAL.labels(direction="yt_to_tg").inc(count)
            await self._reply(update, f"\U0001f504 Reset {count} failed YT\u2192TG tracks.")
        else:
            try:
                track_id = int(arg.lstrip("#"))
            except ValueError:
                await self._reply(update, "\u274c Invalid. Use: /retry &lt;id|all|tg|yt&gt;")
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

        deleted = await self._tracks.delete_track(track_id)
        if deleted:
            await self._reply(update, f"\U0001f5d1 Track #{track_id} deleted.")
        else:
            await self._reply(update, f"\u274c Track #{track_id} not found.")

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
        for i, r in enumerate(results, 1):
            artists = ", ".join(a["name"] for a in r.get("artists", []))
            vid = r.get("videoId", "?")
            lines.append(
                f"{i}. {html.escape(artists)} \u2014 {html.escape(r.get('title', '?'))}\n"
                f"   <code>{vid}</code>"
            )
        await self._reply(update, "\n".join(lines))

    # ── Inline button callbacks ──────────────────────────────────────

    async def _handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not query or not query.from_user or query.from_user.id not in self._admin_ids:
            await query.answer("\u274c Unauthorized")
            return

        data = query.data
        await query.answer()

        if data == "sync_tg_to_yt":
            if self._engine:
                self._engine.force_sync("tg_to_yt")
                await query.message.reply_text(
                    "\U0001f504 TG\u2192YT sync triggered!", parse_mode="HTML"
                )
        elif data == "sync_yt_to_tg":
            if self._engine:
                self._engine.force_sync("yt_to_tg")
                await query.message.reply_text(
                    "\U0001f504 YT\u2192TG sync triggered!", parse_mode="HTML"
                )
        elif data == "show_failed":
            await self._cmd_failed(update, context)
        elif data == "show_stats":
            await self._cmd_stats(update, context)
        elif data == "retry_all":
            count = await self._tracks.reset_all_failed()
            for d in ("tg_to_yt", "yt_to_tg"):
                RETRIES_TOTAL.labels(direction=d).inc(count)
            await query.message.reply_text(
                f"\U0001f504 Reset {count} failed tracks for retry.", parse_mode="HTML"
            )
        elif data.startswith("retry_"):
            track_id = int(data.split("_")[1])
            track = await self._tracks.get_track(track_id)
            if track and track.status == "failed":
                await self._tracks.reset_for_retry(track_id)
                RETRIES_TOTAL.labels(direction=track.direction).inc()
                await query.message.reply_text(
                    f"\U0001f504 Track #{track_id} queued for retry.", parse_mode="HTML"
                )
            else:
                await query.message.reply_text(
                    f"\u274c Track #{track_id} is not in failed state.", parse_mode="HTML"
                )
        elif data.startswith("delete_"):
            track_id = int(data.split("_")[1])
            deleted = await self._tracks.delete_track(track_id)
            if deleted:
                await query.message.reply_text(
                    f"\U0001f5d1 Track #{track_id} deleted.", parse_mode="HTML"
                )
            else:
                await query.message.reply_text(
                    f"\u274c Track #{track_id} not found.", parse_mode="HTML"
                )

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
            "logs": self._cmd_logs,
            "failed": self._cmd_failed,
            "list_failed": self._cmd_failed,  # alias
            "sync": self._cmd_sync,
            "force_sync": self._cmd_sync,  # alias
            "retry": self._cmd_retry,
            "delete": self._cmd_delete,
            "search": self._cmd_search,
        }
        for name, handler in commands.items():
            self._app.add_handler(CommandHandler(name, handler))

        return self._app
