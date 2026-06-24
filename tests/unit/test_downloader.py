from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from navaar.ytmusic.downloader import (
    _MAX_BITRATE_KBPS,
    _MB,
    _MIN_BITRATE_KBPS,
    YTDownloader,
    _target_bitrate_kbps,
)


def _write(path: str, data: bytes) -> None:
    Path(path).write_bytes(data)


def test_target_bitrate_long_track_fits_under_limit() -> None:
    # ~60 min at the 50 MiB limit should land in a normal mp3 range and, crucially,
    # produce a file under the cap.
    max_bytes = 50 * _MB
    kbps = _target_bitrate_kbps(3600, max_bytes)
    assert _MIN_BITRATE_KBPS <= kbps <= _MAX_BITRATE_KBPS
    projected_bytes = kbps * 1000 / 8 * 3600
    assert projected_bytes < max_bytes


def test_target_bitrate_short_track_clamps_to_max() -> None:
    # A short track would allow a huge bitrate; clamp to the ceiling.
    assert _target_bitrate_kbps(120, 50 * _MB) == _MAX_BITRATE_KBPS


def test_target_bitrate_very_long_track_clamps_to_min() -> None:
    # A multi-hour track can't fit at a decent bitrate; fall to the floor.
    assert _target_bitrate_kbps(4 * 3600, 50 * _MB) == _MIN_BITRATE_KBPS


def test_target_bitrate_unknown_duration_uses_floor() -> None:
    assert _target_bitrate_kbps(0, 50 * _MB) == _MIN_BITRATE_KBPS


@pytest.mark.asyncio
async def test_fit_upload_limit_skips_small_files(tmp_path: Path) -> None:
    dl = YTDownloader(download_dir=str(tmp_path), max_upload_mb=50)
    f = tmp_path / "small.mp3"
    f.write_bytes(b"x" * 1024)
    with patch(
        "navaar.ytmusic.downloader.asyncio.create_subprocess_exec"
    ) as spawn:
        result = await dl._fit_upload_limit(f)
    assert result == f
    spawn.assert_not_called()  # no ffprobe/ffmpeg for a file under the limit


@pytest.mark.asyncio
async def test_fit_upload_limit_compresses_oversized(tmp_path: Path) -> None:
    dl = YTDownloader(download_dir=str(tmp_path), max_upload_mb=50)
    big = tmp_path / "vid.mp3"
    big.write_bytes(b"x" * 4096)
    dl._max_upload_bytes = 1024  # force the file to count as oversized

    async def fake_ffmpeg(*cmd, **kwargs):
        # The output path is the last positional arg; create a small "encoded" file.
        _write(cmd[-1], b"y" * 256)
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(b"", b""))
        proc.returncode = 0
        return proc

    with patch.object(dl, "_probe_duration", AsyncMock(return_value=3600.0)), patch(
        "navaar.ytmusic.downloader.asyncio.create_subprocess_exec",
        side_effect=fake_ffmpeg,
    ):
        result = await dl._fit_upload_limit(big)

    assert result != big
    assert result.exists()
    assert not big.exists()  # oversized original is cleaned up
    # bitrate for ~60min/1KiB budget floors out, naming the file accordingly
    assert result.name.endswith(f".{_MIN_BITRATE_KBPS}k.mp3")


@pytest.mark.asyncio
async def test_fit_upload_limit_keeps_original_on_failure(tmp_path: Path) -> None:
    dl = YTDownloader(download_dir=str(tmp_path), max_upload_mb=50)
    big = tmp_path / "vid.mp3"
    big.write_bytes(b"x" * 4096)
    dl._max_upload_bytes = 1024

    async def failing_ffmpeg(*cmd, **kwargs):
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(b"", b"encode boom"))
        proc.returncode = 1
        return proc

    with patch.object(dl, "_probe_duration", AsyncMock(return_value=3600.0)), patch(
        "navaar.ytmusic.downloader.asyncio.create_subprocess_exec",
        side_effect=failing_ffmpeg,
    ):
        result = await dl._fit_upload_limit(big)

    assert result == big  # falls back to the original so the failure is visible
    assert big.exists()
