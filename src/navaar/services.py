from __future__ import annotations

import re

# Presentation for the three services, keyed by the prefix used in direction
# strings ("tg_to_yt" -> "tg"/"yt"). Shared so captions, cards, and commands agree.
SVC_ICON = {"tg": "\U0001f4e8", "yt": "\U0001f3ac", "sp": "\U0001f7e2"}
SVC_LABEL = {"tg": "Telegram", "yt": "YouTube Music", "sp": "Spotify"}

# Every bot-originated audio upload (yt_to_tg / sp_to_tg) carries this marker in its
# caption. Channel posts are attributed to the channel (no from_user), so the bot
# cannot recognise its own uploads by sender — it recognises them by this marker and
# skips them, preventing a re-ingestion loop. The marker travels with the message, so
# it works even if the update arrives before the upload's DB row is committed.
SYNC_CAPTION_MARKER = "Synced by Navaar"


def sync_caption(direction: str, track_id: int) -> str:
    """Caption for a bot-synced upload, e.g. '🟢 via Spotify · Synced by Navaar · #42'.
    Self-describing (shows provenance) and carries SYNC_CAPTION_MARKER for self-skip."""
    prefix = direction.split("_to_")[0]
    icon = SVC_ICON.get(prefix, "")
    label = SVC_LABEL.get(prefix, prefix)
    via = f"{icon} via {label} · " if label else ""
    return f"{via}{SYNC_CAPTION_MARKER} · #{track_id}"


# The exact structured suffix a bot upload's caption ends with (marker + `· #<id>`).
# Matched instead of a bare substring so a human caption that merely mentions the
# marker phrase can't be mistaken for the bot's own upload and silently dropped.
_SYNC_CAPTION_RE = re.compile(re.escape(SYNC_CAPTION_MARKER) + r" · #\d+")


def is_sync_caption(caption: str | None) -> bool:
    """True if a channel post's caption is one the bot itself produced (a synced
    upload), so the ingestion path can skip it without re-syncing its own uploads."""
    return bool(caption) and _SYNC_CAPTION_RE.search(caption) is not None
