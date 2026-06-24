from __future__ import annotations

import asyncio
import html
from collections import defaultdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest

from navaar.db.repository import TrackRepository

if TYPE_CHECKING:
    from telegram import Bot

    from navaar.db.models import Track

logger = structlog.get_logger()

# Service presentation. Keys are the source/target prefixes used in directions.
_SVC_ICON = {"tg": "\U0001f4e8", "yt": "\U0001f3ac", "sp": "\U0001f7e2"}
_SVC_LABEL = {"tg": "Telegram", "yt": "YouTube Music", "sp": "Spotify"}
_SVC_BTN = {"tg": "\U0001f4e8 Telegram", "yt": "▶️ YouTube Music", "sp": "\U0001f7e2 Spotify"}

# Status indicators (mirrors bot._S).
_STATUS_ICON = {
    "pending": "⏳",
    "retry_scheduled": "\U0001f504",
    "identifying": "\U0001f50d",
    "searching": "\U0001f50e",
    "syncing": "⚙️",
    "synced": "✅",
    "failed": "❌",
    "duplicate": "\U0001f501",
}

# Statuses for which the target's external link is meaningful (the track exists
# on the target service / in its playlist).
_LINKED_STATUSES = {"synced", "duplicate"}


def _ago(dt: datetime | None) -> str:
    if not dt:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    secs = int((datetime.now(UTC) - dt).total_seconds())
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


class TrackCardService:
    """Posts and live-edits one status card per *logical* track — a reply, in the
    channel, to the track's audio message. The card shows where the track was first
    seen, its per-platform sync status, and tappable links once they exist.

    Every method is best-effort: a card failure must never break a sync, so
    ``refresh`` swallows all of its own exceptions.
    """

    def __init__(
        self,
        bot: Bot,
        channel_id: int,
        track_repo: TrackRepository,
        *,
        sp_enabled: bool,
        enabled: bool = True,
    ) -> None:
        self._bot = bot
        self._channel_id = channel_id
        self._tracks = track_repo
        self._sp_enabled = sp_enabled
        self._enabled = enabled
        # One lock per logical track so two concurrent sync loops can't both post
        # the first card. Keyed by the origin external id.
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def refresh(self, track_id: int) -> None:
        if not self._enabled:
            return
        try:
            await self._refresh(track_id)
        except Exception:
            logger.warning("track_card_refresh_failed", track_id=track_id, exc_info=True)

    async def _refresh(self, track_id: int) -> None:
        track = await self._tracks.get_track(track_id)
        if not track:
            return
        prefix = track.direction.split("_to_")[0]
        key = self._origin_key(track, prefix)
        if key is None:
            return  # origin id not populated yet — nothing to correlate on

        async with self._locks[key]:
            siblings = await self._tracks.get_sibling_tracks(track)
            anchor = self._anchor_message_id(siblings)
            if anchor is None:
                return  # no audio message in the channel to reply to yet

            text, keyboard = self._render(siblings, prefix)
            card_id = next(
                (s.card_message_id for s in siblings if s.card_message_id), None
            )

            if card_id is None:
                msg = await self._bot.send_message(
                    chat_id=self._channel_id,
                    text=text,
                    reply_to_message_id=anchor,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                    reply_markup=keyboard,
                )
                await self._tracks.set_card_message_id(
                    [s.id for s in siblings], msg.message_id
                )
                logger.info(
                    "track_card_posted",
                    origin=prefix, card_message_id=msg.message_id, anchor=anchor,
                )
            else:
                try:
                    await self._bot.edit_message_text(
                        text=text,
                        chat_id=self._channel_id,
                        message_id=card_id,
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                        reply_markup=keyboard,
                    )
                except BadRequest as e:
                    # An edit that doesn't change anything is a no-op, not an error.
                    if "not modified" not in str(e).lower():
                        raise

    @staticmethod
    def _origin_key(track: Track, prefix: str) -> str | None:
        field = {"tg": "tg_file_id", "yt": "yt_video_id", "sp": "sp_track_id"}.get(prefix)
        value = getattr(track, field) if field else None
        return f"{prefix}:{value}" if value else None

    @staticmethod
    def _anchor_message_id(siblings: list[Track]) -> int | None:
        ids = [s.tg_message_id for s in siblings if s.tg_message_id]
        return min(ids) if ids else None

    def _services(self) -> list[str]:
        return ["tg", "yt", "sp"] if self._sp_enabled else ["tg", "yt"]

    def _render(self, siblings: list[Track], prefix: str) -> tuple[str, InlineKeyboardMarkup | None]:
        by_dir = {s.direction: s for s in siblings}
        primary = siblings[0]  # ordered by id → earliest/source row

        artist = html.escape(primary.artist or "Unknown")
        title = html.escape(primary.title or "Unknown")
        first_seen = _ago(primary.created_at)
        header = f"\U0001f3b5 <b>{artist} — {title}</b>"
        meta = f"First seen on {_SVC_LABEL.get(prefix, prefix)} · <code>#{primary.id}</code>"
        if first_seen:
            meta += f" · {first_seen}"

        lines = [header, meta, ""]
        buttons: list[InlineKeyboardButton] = []

        for svc in self._services():
            icon = _SVC_ICON.get(svc, "•")
            label = _SVC_LABEL.get(svc, svc)
            if svc == prefix:
                lines.append(f"{icon} {label} · <i>source</i>")
                continue
            row = by_dir.get(f"{prefix}_to_{svc}")
            status = row.status if row else "pending"
            lines.append(f"{icon} {label} · {_STATUS_ICON.get(status, '❓')} {status}")
            url = self._target_url(svc, row, status)
            if url:
                buttons.append(InlineKeyboardButton(_SVC_BTN.get(svc, label), url=url))

        keyboard = InlineKeyboardMarkup([buttons]) if buttons else None
        return "\n".join(lines), keyboard

    def _target_url(self, svc: str, row: Track | None, status: str) -> str | None:
        if row is None or status not in _LINKED_STATUSES:
            return None
        if svc == "yt" and row.yt_video_id:
            return f"https://music.youtube.com/watch?v={row.yt_video_id}"
        if svc == "sp" and row.sp_track_id:
            return f"https://open.spotify.com/track/{row.sp_track_id}"
        if svc == "tg" and row.tg_message_id:
            return self._tme_link(row.tg_message_id)
        return None

    def _tme_link(self, message_id: int) -> str | None:
        cid = str(self._channel_id)
        # Private channels/supergroups: -100<internal> → t.me/c/<internal>/<msg>.
        if cid.startswith("-100"):
            return f"https://t.me/c/{cid[4:]}/{message_id}"
        return None
