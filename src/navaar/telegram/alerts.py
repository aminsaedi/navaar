from __future__ import annotations

import html
import time
from dataclasses import dataclass

import structlog
from tenacity import RetryError

from navaar.auth_errors import is_permanent_auth_error, is_quota_error

logger = structlog.get_logger()

_DIR_LABEL = {
    "tg_to_yt": "TG→YT", "yt_to_tg": "YT→TG",
    "tg_to_sp": "TG→SP", "sp_to_tg": "SP→TG",
    "yt_to_sp": "YT→SP", "sp_to_yt": "SP→YT",
}


@dataclass
class _DirState:
    consecutive: int = 0
    alert_open: bool = False
    last_alert_ts: float = 0.0
    signature: str = ""


class AlertNotifier:
    """Pushes systemic sync failures to Telegram with anti-spam.

    A crash loop fires once (at the threshold), then at most one reminder per
    cooldown window, then a single "recovered" message — rather than one message
    per failed cycle. Auth failures (revoked/expired credentials), which never
    self-heal, escalate on the first occurrence. Every public method swallows its
    own exceptions: an alert-send failure during an incident must never become a
    second exception inside the engine's crash handler.
    """

    def __init__(
        self,
        bot: object,
        chat_id: int | None,
        *,
        enabled: bool = True,
        consecutive_threshold: int = 2,
        cooldown_seconds: int = 1800,
    ) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._enabled = enabled and bool(chat_id)
        self._threshold = consecutive_threshold
        self._cooldown = cooldown_seconds
        self._state: dict[str, _DirState] = {}

    @staticmethod
    def _signature(exc: BaseException) -> str:
        first_line = str(exc).splitlines()[0] if str(exc) else ""
        return f"{type(exc).__name__}: {first_line[:160]}"

    @staticmethod
    def _quota_signature(exc: BaseException) -> str:
        # The failing URL carries a per-call query (search terms, page token, video
        # ids); keep only host+path so one quota incident keeps one signature and
        # doesn't re-alert as "changed" on every cycle.
        if isinstance(exc, RetryError):
            exc = exc.last_attempt.exception() or exc
        try:
            url = exc.request.url  # httpx errors carry the request
        except Exception:
            url = None
        where = f" ({url.host}{url.path})" if url is not None else ""
        return f"API quota/rate limit exhausted{where}"

    async def record_crash(self, direction: str, exc: BaseException) -> None:
        """Called from the engine crash handler. Never raises."""
        if not self._enabled:
            return
        try:
            st = self._state.setdefault(direction, _DirState())
            st.consecutive += 1
            quota = is_quota_error(exc)
            sig = self._quota_signature(exc) if quota else self._signature(exc)
            auth = not quota and is_permanent_auth_error(exc)
            threshold = 1 if auth else self._threshold
            now = time.time()

            new_incident = not st.alert_open and st.consecutive >= threshold
            sig_changed = st.alert_open and sig != st.signature
            # A spent quota only clears at its reset (hours away for YouTube), so
            # "still failing" reminders every cooldown are pure noise: alert once,
            # then the "recovered" message closes it.
            cooldown_done = (
                st.alert_open and not quota and (now - st.last_alert_ts) >= self._cooldown
            )

            if new_incident or sig_changed or cooldown_done:
                st.alert_open = True
                st.signature = sig
                st.last_alert_ts = now
                reminder = cooldown_done and not sig_changed and not new_incident
                await self._send(self._format(direction, st, sig, auth, reminder, quota))
        except Exception:
            logger.warning("alert_record_crash_failed", direction=direction, exc_info=True)

    async def record_success(self, direction: str) -> None:
        """Called after a clean cycle. Sends one 'recovered' message if an
        incident was open, then resets state. Never raises."""
        if not self._enabled:
            return
        try:
            st = self._state.get(direction)
            if st and st.alert_open:
                label = _DIR_LABEL.get(direction, direction)
                await self._send(f"✅ <b>{label}</b> recovered — syncing normally again.")
            if st:
                st.consecutive = 0
                st.alert_open = False
                st.signature = ""
        except Exception:
            logger.warning("alert_record_success_failed", direction=direction, exc_info=True)

    def _format(self, direction, st, sig, auth, reminder, quota=False) -> str:
        label = _DIR_LABEL.get(direction, direction)
        if reminder:
            head = "🔁 STILL FAILING"
        elif quota:
            head = "⏳ API QUOTA EXHAUSTED"
        elif auth:
            head = "🔐 AUTH FAILURE"
        else:
            head = "🚨 SYNC FAILURE"
        if quota:
            hint = (
                "\nThe service's API quota/rate limit is spent — no re-auth needed. "
                "Pending tracks wait and resume on their own (YouTube's daily quota "
                "resets at midnight Pacific)."
            )
        elif auth:
            hint = "\nLikely a revoked/expired token — re-auth required."
        else:
            hint = ""
        return (
            f"<b>{head}: {label}</b>\n"
            f"Consecutive crashes: <b>{st.consecutive}</b>\n"
            f"<code>{html.escape(sig)}</code>{hint}"
        )

    async def _send(self, text: str) -> None:
        # Single attempt with generous timeouts; no tenacity retry (it would block
        # the loop). On failure the incident stays open, so the next cooldown
        # window naturally retries.
        await self._bot.send_message(
            chat_id=self._chat_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            read_timeout=30,
            write_timeout=30,
            connect_timeout=15,
        )
