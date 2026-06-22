from __future__ import annotations

from tenacity import retry_if_exception

# Substrings that indicate a permanent auth failure requiring operator re-auth.
_AUTH_STRINGS = ("invalid_grant", "revoked", "invalid_token", "unauthorized")
# HTTP statuses that never succeed on retry (bad request / expired / forbidden).
_PERMANENT_STATUS = (400, 401, 403)


def _status_of(exc: BaseException) -> int | None:
    # spotipy.SpotifyException exposes .http_status; httpx.HTTPStatusError nests
    # the status under .response.status_code.
    status = getattr(exc, "http_status", None)
    if status is None:
        resp = getattr(exc, "response", None)
        status = getattr(resp, "status_code", None)
    return status


def is_permanent_auth_error(exc: BaseException) -> bool:
    """True for auth failures that can never succeed on retry (revoked/expired
    credentials, forbidden). Retrying these only wastes attempts and backoff,
    and — because the clients are synchronous — that backoff blocks the loop."""
    if _status_of(exc) in _PERMANENT_STATUS:
        return True
    text = str(exc).lower()
    return any(s in text for s in _AUTH_STRINGS)


def classify_auth_service(exc: BaseException) -> str | None:
    """Map a permanent auth error to the external service it came from
    ("sp" | "yt"), or None if it isn't a permanent auth error."""
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


# tenacity predicate: retry everything EXCEPT permanent auth errors.
retry_if_transient = retry_if_exception(lambda e: not is_permanent_auth_error(e))
