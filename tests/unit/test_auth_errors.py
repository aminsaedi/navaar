from __future__ import annotations

import httpx

from navaar.auth_errors import (
    classify_auth_service,
    is_permanent_auth_error,
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
