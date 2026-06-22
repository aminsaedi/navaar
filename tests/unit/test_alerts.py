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
