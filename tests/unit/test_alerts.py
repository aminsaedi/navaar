from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest

from navaar.telegram.alerts import AlertNotifier


def _bot() -> AsyncMock:
    bot = AsyncMock()
    bot.send_message = AsyncMock()
    return bot


def _auth_exc() -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "https://api.spotify.com/v1/me")
    resp = httpx.Response(401, request=req)
    return httpx.HTTPStatusError("invalid_grant: revoked", request=req, response=resp)


@pytest.mark.asyncio
async def test_generic_crash_alerts_only_after_threshold():
    bot = _bot()
    n = AlertNotifier(bot, chat_id=123, consecutive_threshold=2)

    await n.record_crash("tg_to_yt", RuntimeError("boom"))
    bot.send_message.assert_not_called()  # first crash: below threshold

    await n.record_crash("tg_to_yt", RuntimeError("boom"))
    bot.send_message.assert_awaited_once()  # second crash: fires


@pytest.mark.asyncio
async def test_auth_error_escalates_on_first_crash():
    bot = _bot()
    n = AlertNotifier(bot, chat_id=123, consecutive_threshold=2)

    await n.record_crash("sp_to_tg", _auth_exc())
    bot.send_message.assert_awaited_once()  # auth => threshold 1
    assert "AUTH" in bot.send_message.call_args.kwargs["text"]


@pytest.mark.asyncio
async def test_no_spam_while_incident_open():
    bot = _bot()
    n = AlertNotifier(bot, chat_id=123, consecutive_threshold=1, cooldown_seconds=10_000)

    for _ in range(5):
        await n.record_crash("sp_to_tg", RuntimeError("boom"))

    # One alert for the open incident, not one per cycle.
    assert bot.send_message.await_count == 1


@pytest.mark.asyncio
async def test_recovery_sends_resolved_then_resets():
    bot = _bot()
    n = AlertNotifier(bot, chat_id=123, consecutive_threshold=1)

    await n.record_crash("sp_to_tg", RuntimeError("boom"))
    assert bot.send_message.await_count == 1

    await n.record_success("sp_to_tg")
    assert bot.send_message.await_count == 2
    assert "recovered" in bot.send_message.call_args.kwargs["text"].lower()

    # A healthy direction with no open incident stays silent.
    await n.record_success("sp_to_tg")
    assert bot.send_message.await_count == 2


@pytest.mark.asyncio
async def test_disabled_when_no_chat_id():
    bot = _bot()
    n = AlertNotifier(bot, chat_id=None)
    await n.record_crash("sp_to_tg", _auth_exc())
    bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_send_failure_never_raises():
    bot = _bot()
    bot.send_message = AsyncMock(side_effect=RuntimeError("telegram down"))
    n = AlertNotifier(bot, chat_id=123, consecutive_threshold=1)
    # Must not propagate — the alert path cannot become a second exception.
    await n.record_crash("sp_to_tg", RuntimeError("boom"))


def _quota_exc() -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "https://www.googleapis.com/youtube/v3/playlistItems")
    resp = httpx.Response(
        403, request=req, json={"error": {"errors": [{"reason": "quotaExceeded"}]}}
    )
    return httpx.HTTPStatusError("Client error '403 Forbidden'", request=req, response=resp)


@pytest.mark.asyncio
async def test_quota_exhaustion_is_not_reported_as_auth_failure():
    bot = _bot()
    n = AlertNotifier(bot, chat_id=123, consecutive_threshold=2)

    await n.record_crash("yt_to_tg", _quota_exc())
    bot.send_message.assert_not_called()  # not escalated like a revoked token

    await n.record_crash("yt_to_tg", _quota_exc())
    text = bot.send_message.call_args.kwargs["text"]
    assert "QUOTA" in text
    assert "AUTH" not in text and "re-auth required" not in text


@pytest.mark.asyncio
async def test_quota_incident_alerts_once_then_recovers(monkeypatch):
    """Overnight, a spent YouTube quota sent a 'STILL FAILING' reminder every 30
    minutes until the reset. One alert + one 'recovered' is the whole story."""
    import navaar.telegram.alerts as alerts_mod

    bot = _bot()
    n = AlertNotifier(bot, chat_id=123, consecutive_threshold=1, cooldown_seconds=1800)
    clock = [1_000_000.0]
    monkeypatch.setattr(alerts_mod.time, "time", lambda: clock[0])

    for page in range(12):  # six hours of 30-minute backoff crashes
        req = httpx.Request(
            "GET",
            f"https://www.googleapis.com/youtube/v3/playlistItems?pageToken=p{page}",
        )
        resp = httpx.Response(
            403, request=req, json={"error": {"errors": [{"reason": "quotaExceeded"}]}}
        )
        await n.record_crash(
            "yt_to_tg", httpx.HTTPStatusError("403", request=req, response=resp)
        )
        clock[0] += 1900

    assert bot.send_message.await_count == 1
    assert "QUOTA" in bot.send_message.call_args.kwargs["text"]

    await n.record_success("yt_to_tg")
    assert bot.send_message.await_count == 2
    assert "recovered" in bot.send_message.call_args.kwargs["text"]
