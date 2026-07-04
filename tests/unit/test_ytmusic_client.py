from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from navaar.ytmusic.client import YTMusicClient


def _write_token(path: Path, *, expires_at: float) -> None:
    path.write_text(
        json.dumps(
            {
                "access_token": "at",
                "refresh_token": "rt",
                "expires_at": expires_at,
                "expires_in": 3600,
            }
        )
    )


@pytest.fixture
def fresh_client(tmp_path: Path) -> YTMusicClient:
    auth = tmp_path / "oauth.json"
    _write_token(auth, expires_at=time.time() + 100000)  # far future -> no refresh
    return YTMusicClient(
        auth_file=str(auth), playlist_id="PL", client_id="cid", client_secret="sec"
    )


def test_no_refresh_when_token_fresh(fresh_client: YTMusicClient) -> None:
    # Should not raise / attempt a network refresh for a token valid for hours.
    assert fresh_client.get_access_token() == "at"


def test_save_token_is_atomic_and_valid(fresh_client: YTMusicClient, tmp_path: Path) -> None:
    fresh_client._token["access_token"] = "new-at"
    fresh_client._save_token()
    auth = tmp_path / "oauth.json"
    # File parses cleanly (no half-written JSON) and no temp file is left behind.
    reloaded = json.loads(auth.read_text())
    assert reloaded["access_token"] == "new-at"
    assert not (tmp_path / "oauth.json.tmp").exists()


def test_ensure_fresh_double_checked(monkeypatch, fresh_client: YTMusicClient) -> None:
    # Token already fresh: _refresh_token must never be called (double-checked guard).
    calls = {"n": 0}

    def _boom() -> None:
        calls["n"] += 1

    monkeypatch.setattr(fresh_client, "_refresh_token", _boom)
    fresh_client._ensure_fresh_token()
    assert calls["n"] == 0
