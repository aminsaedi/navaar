from __future__ import annotations

import httpx

from navaar.auth_errors import (
    classify_auth_service,
    is_permanent_auth_error,
    is_quota_error,
    retry_if_transient,
)


class _SpotifyOauthError(Exception):
    # Mimics spotipy.exceptions.SpotifyOauthError's module for classification.
    __module__ = "spotipy.exceptions"


def _httpx_status_error(status: int, url: str = "https://oauth2.googleapis.com/token"):
    req = httpx.Request("POST", url)
    resp = httpx.Response(status, request=req)
    return httpx.HTTPStatusError(str(status), request=req, response=resp)


def test_revoked_refresh_token_is_permanent():
    exc = _SpotifyOauthError("error: invalid_grant, Refresh token revoked")
    assert is_permanent_auth_error(exc) is True
    assert classify_auth_service(exc) == "sp"


def test_http_401_is_permanent_yt():
    exc = _httpx_status_error(401)
    assert is_permanent_auth_error(exc) is True
    assert classify_auth_service(exc) == "yt"


def test_http_403_is_permanent():
    assert is_permanent_auth_error(_httpx_status_error(403)) is True


def test_transient_errors_are_retried():
    # 500 and 429 are transient: not permanent, predicate says "retry".
    for status in (429, 500, 503):
        exc = _httpx_status_error(status)
        assert is_permanent_auth_error(exc) is False
        assert classify_auth_service(exc) is None
    assert is_permanent_auth_error(ConnectionError("network")) is False


def test_retry_predicate_skips_permanent_auth():
    # tenacity predicate: True => retry. Permanent auth => do NOT retry.
    assert retry_if_transient.predicate(_httpx_status_error(500)) is True
    assert retry_if_transient.predicate(_httpx_status_error(401)) is False


def _google_quota_error(status: int = 403, reason: str = "quotaExceeded"):
    url = "https://www.googleapis.com/youtube/v3/playlistItems?part=snippet"
    req = httpx.Request("GET", url)
    body = {"error": {"code": status, "errors": [{"reason": reason, "domain": "youtube.quota"}]}}
    resp = httpx.Response(status, request=req, json=body)
    return httpx.HTTPStatusError(f"Client error '{status} Forbidden'", request=req, response=resp)


def test_youtube_quota_403_is_not_an_auth_failure():
    # The YouTube Data API reports a spent daily quota as 403; it must not page the
    # operator to re-auth, and retrying it only repeats the refusal.
    exc = _google_quota_error()
    assert is_quota_error(exc) is True
    assert is_permanent_auth_error(exc) is False
    assert classify_auth_service(exc) is None
    assert retry_if_transient.predicate(exc) is False


def test_rate_limit_reasons_and_429_are_quota():
    assert is_quota_error(_google_quota_error(reason="rateLimitExceeded")) is True
    assert is_quota_error(_httpx_status_error(429)) is True
    assert retry_if_transient.predicate(_httpx_status_error(429)) is False


def test_plain_403_is_still_auth_not_quota():
    exc = _httpx_status_error(403)
    assert is_quota_error(exc) is False
    assert is_permanent_auth_error(exc) is True


def test_quota_error_seen_through_tenacity_retry_error():
    from tenacity import Future, RetryError

    fut = Future(attempt_number=3)
    fut.set_exception(_google_quota_error())
    assert is_quota_error(RetryError(fut)) is True
    assert is_quota_error(ConnectionError("network")) is False


def test_google_daily_quota_waits_until_midnight_pacific():
    from datetime import UTC, datetime

    from navaar.auth_errors import quota_retry_after

    # 14:00 UTC on 23 Nov = 06:00 PST; the quota resets at 08:00 UTC on the 24th.
    now = datetime(2026, 11, 23, 14, 0, tzinfo=UTC)
    wait = quota_retry_after(_google_quota_error(), now=now)
    assert wait == 18 * 3600 + 60

    # Across the spring DST change (PDT, UTC-7) the reset is 07:00 UTC.
    now = datetime(2026, 7, 1, 6, 0, tzinfo=UTC)
    assert quota_retry_after(_google_quota_error(), now=now) == 3600 + 60


def test_quota_retry_after_honours_header_and_short_limits():
    from navaar.auth_errors import quota_retry_after

    req = httpx.Request("GET", "https://api.spotify.com/v1/search")
    resp = httpx.Response(429, request=req, headers={"Retry-After": "7"})
    exc = httpx.HTTPStatusError("429", request=req, response=resp)
    assert quota_retry_after(exc) == 7.0

    per_minute = _google_quota_error(
        status=429, reason="Quota exceeded for 'Search Queries per minute'"
    )
    assert quota_retry_after(per_minute) == 120.0
    # Non-Google rate limit without a header: a short pause, not a day.
    assert quota_retry_after(_httpx_status_error(429, url="https://api.spotify.com/v1/x")) == 300.0
