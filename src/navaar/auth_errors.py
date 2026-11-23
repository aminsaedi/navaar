from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from tenacity import RetryError, retry_if_exception

# Google API quotas (YouTube Data API) reset at midnight Pacific.
_PACIFIC = ZoneInfo("America/Los_Angeles")

# Substrings that indicate a permanent auth failure requiring operator re-auth.
_AUTH_STRINGS = ("invalid_grant", "revoked", "invalid_token", "unauthorized")
# HTTP statuses that never succeed on retry (bad request / expired / forbidden).
_PERMANENT_STATUS = (400, 401, 403)
# Google API error reasons (in the JSON body) for an exhausted quota or rate limit.
# The YouTube Data API reports its daily quota as a *403*, so without these a spent
# quota reads as a revoked token and pages the operator to re-auth for nothing.
_QUOTA_REASONS = (
    "quotaexceeded",
    "dailylimitexceeded",
    "ratelimitexceeded",
    "userratelimitexceeded",
    "resource_exhausted",
)


def _status_of(exc: BaseException) -> int | None:
    # spotipy.SpotifyException exposes .http_status; httpx.HTTPStatusError nests
    # the status under .response.status_code.
    status = getattr(exc, "http_status", None)
    if status is None:
        resp = getattr(exc, "response", None)
        status = getattr(resp, "status_code", None)
    return status


def _unwrap(exc: BaseException) -> BaseException:
    # A tenacity RetryError hides the real failure behind its last attempt.
    if isinstance(exc, RetryError):
        inner = exc.last_attempt.exception()
        if inner is not None:
            return inner
    return exc


def is_quota_error(exc: BaseException) -> bool:
    """True when an API refused the call for quota/rate-limit reasons: HTTP 429,
    or a 403 whose body names a quota reason (YouTube's daily quota). These heal
    on their own (the YouTube quota resets at midnight Pacific) and need no re-auth;
    every further call fails the same way until then."""
    exc = _unwrap(exc)
    status = _status_of(exc)
    if status == 429:
        return True
    if status != 403:
        return False
    haystack = _quota_text(exc)
    return any(r in haystack for r in _QUOTA_REASONS)


def _quota_text(exc: BaseException) -> str:
    resp = getattr(exc, "response", None)
    body = ""
    if resp is not None:
        try:
            body = resp.text
        except Exception:
            body = ""
    url = ""
    try:
        url = str(exc.request.url)  # httpx errors carry the request
    except Exception:
        pass
    return f"{body} {exc} {url}".lower()


def quota_retry_after(exc: BaseException, now: datetime | None = None) -> float:
    """Seconds until a quota-refused call is worth trying again.

    A ``Retry-After`` header wins. Otherwise a Google per-day quota (YouTube's
    10k units/day, or its separate 100 ``search.list`` calls/day) only comes back
    at midnight Pacific, so wait for that; a per-minute limit or a non-Google
    rate limit gets a short pause.
    """
    exc = _unwrap(exc)
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if headers is None:
        headers = getattr(exc, "headers", None)  # spotipy.SpotifyException
    try:
        retry_after = (headers or {}).get("Retry-After")
        if retry_after is not None:
            return max(float(retry_after), 1.0)
    except (TypeError, ValueError, AttributeError):
        pass

    text = _quota_text(exc)
    if "googleapis" in text and "minute" not in text:
        now_utc = (now or datetime.now(UTC)).astimezone(UTC)
        local = now_utc.astimezone(_PACIFIC)
        midnight = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        # Subtract in UTC: aware datetimes sharing a tzinfo subtract as wall-clock
        # time, which is an hour off across a DST change.
        return (midnight.astimezone(UTC) - now_utc).total_seconds() + 60
    if "googleapis" in text:
        return 120.0
    return 300.0


def is_permanent_auth_error(exc: BaseException) -> bool:
    """True for auth failures that can never succeed on retry (revoked/expired
    credentials, forbidden). Retrying these only wastes attempts and backoff,
    and — because the clients are synchronous — that backoff blocks the loop."""
    exc = _unwrap(exc)
    if is_quota_error(exc):
        return False
    if _status_of(exc) in _PERMANENT_STATUS:
        return True
    text = str(exc).lower()
    return any(s in text for s in _AUTH_STRINGS)


def classify_auth_service(exc: BaseException) -> str | None:
    """Map a permanent auth error to the external service it came from
    ("sp" | "yt"), or None if it isn't a permanent auth error."""
    exc = _unwrap(exc)
    if not is_permanent_auth_error(exc):
        return None
    parts = [(type(exc).__module__ or ""), str(exc)]
    # httpx errors carry the target URL on the request, not in str(exc).
    request = getattr(getattr(exc, "request", None), "url", None)
    if request is not None:
        parts.append(str(request))
    haystack = " ".join(parts).lower()
    # "spoti" matches both the spotipy module and "spotify" in messages.
    if "spoti" in haystack:
        return "sp"
    if "google" in haystack or "youtube" in haystack:
        return "yt"
    return None


# tenacity predicate: retry everything EXCEPT permanent auth errors and spent
# quotas. Retrying either just repeats the refusal while blocking a worker thread;
# a quota error goes straight to the caller, which stops the cycle and lets the
# engine back off.
retry_if_transient = retry_if_exception(
    lambda e: not is_permanent_auth_error(e) and not is_quota_error(e)
)
