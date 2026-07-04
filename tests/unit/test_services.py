from __future__ import annotations

import pytest

from navaar.services import SYNC_CAPTION_MARKER, is_sync_caption, sync_caption


def test_sync_caption_is_self_describing_and_marked() -> None:
    cap = sync_caption("sp_to_tg", 42)
    assert "via Spotify" in cap
    assert SYNC_CAPTION_MARKER in cap
    assert cap.endswith("#42")


@pytest.mark.parametrize("direction,label", [("yt_to_tg", "YouTube Music"), ("sp_to_tg", "Spotify")])
def test_sync_caption_labels_source(direction: str, label: str) -> None:
    assert label in sync_caption(direction, 1)


def test_is_sync_caption_matches_own_uploads() -> None:
    assert is_sync_caption(sync_caption("yt_to_tg", 7))
    assert is_sync_caption(sync_caption("sp_to_tg", 12345))


def test_is_sync_caption_ignores_human_captions_mentioning_the_phrase() -> None:
    # A human caption that merely mentions the brand phrase must NOT be treated as a
    # self-upload (which would silently drop the post) — the structured `· #<id>`
    # suffix is required.
    assert not is_sync_caption("Great track — Synced by Navaar community pick")
    assert not is_sync_caption("Synced by Navaar")  # no id suffix
    assert not is_sync_caption(None)
    assert not is_sync_caption("")
    assert not is_sync_caption("just a normal song caption")
