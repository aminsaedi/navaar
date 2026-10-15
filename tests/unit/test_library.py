from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from navaar.db.repository import TrackRepository
from navaar.library import audio
from navaar.library.naming import clean_artist, clean_title, track_stem, unique_file_name
from navaar.library.sync import MANIFEST_KEY, LibrarySync
from navaar.telegram.client import FileTooBigError

HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


# ── fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def library_dir(tmp_path: Path) -> Path:
    d = tmp_path / "navaar"
    d.mkdir()
    return d


@pytest.fixture
def mock_tg() -> MagicMock:
    async def download(file_id: str, dest_dir: str | None = None) -> str:
        path = Path(dest_dir) / f"{file_id}.mp3"
        path.write_bytes(b"tg-audio")
        return str(path)

    client = MagicMock()
    client.download_file = AsyncMock(side_effect=download)
    return client


@pytest.fixture
def mock_dl(tmp_path: Path) -> MagicMock:
    yt_dir = tmp_path / "yt"
    yt_dir.mkdir()

    async def download(video_id: str) -> str:
        path = yt_dir / f"{video_id}.mp3"
        path.write_bytes(b"yt-audio")
        return str(path)

    dl = MagicMock()
    dl.download = AsyncMock(side_effect=download)
    return dl


@pytest.fixture(autouse=True)
def fake_audio(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace ffmpeg work with byte copies so sync logic is tested in isolation."""

    def copy_tagged(src: Path, dest: Path, navaar_id: str) -> None:
        dest.write_bytes(src.read_bytes() + f"|{navaar_id}".encode())

    async def build_mp3(src: Path, dest: Path, **kw) -> None:
        copy_tagged(src, dest, kw["navaar_id"])

    async def no_cover(*a, **k):
        return None

    monkeypatch.setattr(audio, "build_mp3", build_mp3)
    monkeypatch.setattr(audio, "extract_cover", no_cover)
    monkeypatch.setattr(audio, "fetch_youtube_cover", no_cover)
    monkeypatch.setattr(audio, "retag", lambda p, **kw: None)
    monkeypatch.setattr(
        audio, "read_navaar_id",
        lambda p: p.read_bytes().split(b"|")[-1].decode() if b"|" in p.read_bytes() else None,
    )


def make_sync(track_repo, sync_state_repo, mock_tg, mock_dl, library_dir, **kw) -> LibrarySync:
    return LibrarySync(track_repo, sync_state_repo, mock_tg, mock_dl, str(library_dir), **kw)


async def add_channel_track(repo: TrackRepository, msg_id: int, title: str, **kw):
    return await repo.create_track(
        direction=kw.pop("direction", "tg_to_yt"), status=kw.pop("status", "synced"),
        title=title, tg_message_id=msg_id, **kw,
    )


# ── naming ───────────────────────────────────────────────────────────────


def test_track_stem_is_fat32_safe() -> None:
    assert track_stem("AC/DC", 'Who? "Me"*') == "AC DC - Who Me"
    assert track_stem(None, "Solo") == "Solo"
    assert track_stem("Moein", "پروردگارا") == "Moein - پروردگارا"
    assert track_stem("X", "dots...") == "X - dots"
    assert len(track_stem("a" * 300, "b")) <= 100


@pytest.mark.parametrize(
    ("artist", "title", "expected"),
    [
        ("Adele", "Adele - Hello (Official Music Video)", "Hello"),
        ("Moein", "Moein - Parvardegara معین پروردگارا", "Parvardegara معین پروردگارا"),
        ("Ebi", "Ebi | Khalij [Official Video]", "Khalij"),
        ("Queen", "Bohemian Rhapsody (Live) (Lyrics)", "Bohemian Rhapsody (Live)"),
        ("Queen", "Bohemian Rhapsody (Remastered 2011)", "Bohemian Rhapsody (Remastered 2011)"),
        ("Sia", "Sia", "Sia"),  # title == artist: keep, don't empty it
        (None, "Some Song (Audio)", "Some Song"),
        ("Habib", "Marge Ghoo", "Marge Ghoo"),
    ],
)
def test_clean_title(artist, title, expected) -> None:
    assert clean_title(artist, title) == expected


def test_clean_artist_drops_youtube_topic_suffix() -> None:
    assert clean_artist("Siavash Ghomayshi - Topic") == "Siavash Ghomayshi"
    assert clean_artist("Ebi") == "Ebi"
    assert clean_artist(None) is None
    assert clean_title(clean_artist("Ebi - Topic"), "Ebi - Khalij") == "Khalij"


def test_unique_file_name_is_case_insensitive() -> None:
    assert unique_file_name("A - B", {"a - b.mp3"}) == "A - B (2).mp3"
    assert unique_file_name("A - B", {"A - B.mp3", "A - B (2).mp3"}) == "A - B (3).mp3"


# ── planning ─────────────────────────────────────────────────────────────


async def test_channel_tracks_exclude_duplicate_unsynced_and_non_channel(
    track_repo: TrackRepository,
) -> None:
    await add_channel_track(track_repo, 1, "Keep")
    await add_channel_track(track_repo, 2, "Dup", direction="yt_to_tg", status="duplicate")
    await add_channel_track(track_repo, 3, "Gone", direction="yt_to_tg", status="unsynced")
    await track_repo.create_track(direction="tg_to_sp", status="synced", title="Sibling")
    titles = [t.title for t in await track_repo.get_channel_tracks()]
    assert titles == ["Keep"]


async def test_plan_dedupes_same_song_posted_twice(track_repo: TrackRepository) -> None:
    await add_channel_track(track_repo, 1, "Song", artist="A", yt_video_id="v1")
    await add_channel_track(track_repo, 2, "song ", artist="a", direction="yt_to_tg")
    await add_channel_track(track_repo, 3, "Other", artist="B", direction="yt_to_tg",
                            yt_video_id="v1")
    await add_channel_track(track_repo, 4, "Fresh", artist="C")
    items = LibrarySync.plan(await track_repo.get_channel_tracks())
    assert [i.key for i in items] == ["1", "4"]


# ── cycle ────────────────────────────────────────────────────────────────


async def test_cycle_builds_files_manifest_and_playlist(
    track_repo, sync_state_repo, mock_tg, mock_dl, library_dir: Path
) -> None:
    await add_channel_track(track_repo, 10, "Tg Song", artist="A", tg_file_id="f1")
    await add_channel_track(track_repo, 11, "Yt Song", artist="B", direction="yt_to_tg",
                            yt_video_id="v2")
    sync = make_sync(track_repo, sync_state_repo, mock_tg, mock_dl, library_dir)

    assert await sync.process_pending() == 2

    assert (library_dir / "A - Tg Song.mp3").read_bytes() == b"tg-audio|10"
    assert (library_dir / "B - Yt Song.mp3").read_bytes() == b"yt-audio|11"
    manifest = await sync_state_repo.get_json(MANIFEST_KEY)
    assert manifest["10"] == {"file": "A - Tg Song.mp3", "source": "telegram", "meta": "A\x1fTg Song"}
    assert manifest["11"] == {"file": "B - Yt Song.mp3", "source": "youtube", "meta": "B\x1fYt Song"}
    playlist = (library_dir / "Navaar.m3u8").read_text(encoding="utf-8").splitlines()
    assert playlist[0] == "#EXTM3U"
    assert [line for line in playlist if line.endswith(".mp3")] == [
        "A - Tg Song.mp3", "B - Yt Song.mp3",
    ]
    assert not (library_dir / ".navaar-tmp").exists()

    # Second cycle is a no-op.
    assert await sync.process_pending() == 0
    assert mock_tg.download_file.await_count == 1


async def test_removed_track_deletes_file(
    track_repo, sync_state_repo, mock_tg, mock_dl, library_dir: Path
) -> None:
    t = await add_channel_track(track_repo, 10, "Bye", artist="A", tg_file_id="f1")
    sync = make_sync(track_repo, sync_state_repo, mock_tg, mock_dl, library_dir)
    await sync.process_pending()
    assert (library_dir / "A - Bye.mp3").exists()

    await track_repo.delete_track(t.id)
    await sync.process_pending()

    assert not (library_dir / "A - Bye.mp3").exists()
    assert await sync_state_repo.get_json(MANIFEST_KEY) == {}
    assert "A - Bye.mp3" not in (library_dir / "Navaar.m3u8").read_text()


async def test_lost_manifest_adopts_existing_files_and_drops_strays(
    track_repo, sync_state_repo, mock_tg, mock_dl, library_dir: Path
) -> None:
    await add_channel_track(track_repo, 10, "Keep", artist="A", tg_file_id="f1")
    (library_dir / "renamed by hand.mp3").write_bytes(b"old|10")
    (library_dir / "stranger.mp3").write_bytes(b"not ours")
    sync = make_sync(track_repo, sync_state_repo, mock_tg, mock_dl, library_dir)

    assert await sync.process_pending() == 0  # adopted, nothing downloaded

    mock_tg.download_file.assert_not_awaited()
    assert (library_dir / "renamed by hand.mp3").exists()
    assert not (library_dir / "stranger.mp3").exists()
    manifest = await sync_state_repo.get_json(MANIFEST_KEY)
    assert manifest["10"]["file"] == "renamed by hand.mp3"


async def test_file_deleted_by_hand_is_rebuilt(
    track_repo, sync_state_repo, mock_tg, mock_dl, library_dir: Path
) -> None:
    await add_channel_track(track_repo, 10, "Again", artist="A", tg_file_id="f1")
    sync = make_sync(track_repo, sync_state_repo, mock_tg, mock_dl, library_dir)
    await sync.process_pending()
    (library_dir / "A - Again.mp3").unlink()

    assert await sync.process_pending() == 1
    assert (library_dir / "A - Again.mp3").exists()


async def test_too_big_for_bot_api_falls_back_to_youtube(
    track_repo, sync_state_repo, mock_tg, mock_dl, library_dir: Path
) -> None:
    mock_tg.download_file = AsyncMock(side_effect=FileTooBigError("File is too big"))
    await add_channel_track(track_repo, 10, "Long", artist="A", tg_file_id="f1",
                            yt_video_id="v1")
    sync = make_sync(track_repo, sync_state_repo, mock_tg, mock_dl, library_dir)

    assert await sync.process_pending() == 1
    assert (await sync_state_repo.get_json(MANIFEST_KEY))["10"]["source"] == "youtube"


async def test_failure_backs_off_without_blocking_others(
    track_repo, sync_state_repo, mock_tg, mock_dl, library_dir: Path
) -> None:
    mock_dl.download = AsyncMock(side_effect=RuntimeError("HTTP Error 403"))
    await add_channel_track(track_repo, 10, "Broken", artist="A", direction="yt_to_tg",
                            yt_video_id="v1")
    await add_channel_track(track_repo, 11, "Fine", artist="B", tg_file_id="f2")
    sync = make_sync(track_repo, sync_state_repo, mock_tg, mock_dl, library_dir)

    assert await sync.process_pending() == 1
    manifest = await sync_state_repo.get_json(MANIFEST_KEY)
    assert manifest["10"]["file"] is None
    assert manifest["10"]["attempts"] == 1
    assert "403" in manifest["10"]["error"]
    assert manifest["11"]["file"] == "B - Fine.mp3"

    # Still inside the backoff window: not retried.
    await sync.process_pending()
    assert mock_dl.download.await_count == 1

    status = await sync.status()
    assert status["present"] == 1 and len(status["failed"]) == 1


async def test_max_per_cycle_bounds_first_fill(
    track_repo, sync_state_repo, mock_tg, mock_dl, library_dir: Path
) -> None:
    for i in range(5):
        await add_channel_track(track_repo, 100 + i, f"S{i}", artist="A", tg_file_id=f"f{i}")
    sync = make_sync(track_repo, sync_state_repo, mock_tg, mock_dl, library_dir, max_per_cycle=2)
    assert [await sync.process_pending() for _ in range(4)] == [2, 2, 1, 0]


async def test_missing_library_dir_fails_the_cycle(
    track_repo, sync_state_repo, mock_tg, mock_dl, tmp_path: Path
) -> None:
    sync = make_sync(track_repo, sync_state_repo, mock_tg, mock_dl, tmp_path / "absent")
    with pytest.raises(RuntimeError, match="does not exist"):
        await sync.process_pending()


# ── real ffmpeg ──────────────────────────────────────────────────────────


def _make_media(tmp_path: Path) -> tuple[Path, Path]:
    """A 2 s AAC tone and a 16:9 PNG, generated with ffmpeg."""
    src = tmp_path / "in.m4a"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=f=440:d=2",
         "-c:a", "aac", str(src)], check=True,
    )
    img = tmp_path / "art.png"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=800x450",
         "-frames:v", "1", str(img)], check=True,
    )
    return src, img


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
async def test_build_mp3_writes_car_friendly_tags(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.undo()  # use the real audio helpers
    from mutagen.id3 import ID3
    from mutagen.mp3 import MP3

    src, img = _make_media(tmp_path)
    cover = await audio._square_jpeg(img, tmp_path / "cover.jpg", from_audio=False)
    assert cover is not None

    dest = tmp_path / "out.mp3"
    await audio.build_mp3(
        src, dest, title="پروردگارا", artist="Moein", album="Navaar",
        navaar_id="42", cover=cover, bitrate_kbps=320,
    )

    tags = ID3(dest)
    assert tags.version[:2] == (2, 3)
    assert str(tags["TIT2"]) == "پروردگارا"
    assert str(tags["TPE1"]) == "Moein"
    assert str(tags["TALB"]) == "Navaar"
    apic = tags.getall("APIC")[0]
    assert apic.type == 3 and apic.mime == "image/jpeg"
    assert audio.read_navaar_id(dest) == "42"
    info = MP3(dest).info
    assert info.bitrate == 320000

    # An MP3 source is stream-copied, not re-encoded.
    again = tmp_path / "again.mp3"
    await audio.build_mp3(
        dest, again, title="t", artist=None, album="Navaar", navaar_id="43",
        cover=None, bitrate_kbps=128,
    )
    assert MP3(again).info.bitrate == 320000
    assert audio.read_navaar_id(again) == "43"

    audio.retag(again, title="New Title", artist="New Artist")
    tags = ID3(again)
    assert tags.version[:2] == (2, 3)
    assert str(tags["TIT2"]) == "New Title" and str(tags["TPE1"]) == "New Artist"
    assert audio.read_navaar_id(again) == "43"  # identity survives the retag


async def test_metadata_change_retags_and_renames_without_download(
    track_repo, sync_state_repo, mock_tg, mock_dl, library_dir: Path, monkeypatch
) -> None:
    retagged = []
    monkeypatch.setattr(audio, "retag", lambda p, **kw: retagged.append((p.name, kw)))
    t = await add_channel_track(track_repo, 10, "Golaye Pooneh",
                                artist="Siavash Ghomayshi - Topic", tg_file_id="f1")
    sync = make_sync(track_repo, sync_state_repo, mock_tg, mock_dl, library_dir)
    await sync.process_pending()
    assert (library_dir / "Siavash Ghomayshi - Golaye Pooneh.mp3").exists()
    assert retagged == []

    await track_repo.update_track(t.id, artist="Siavash Ghomayshi", title="Golaye Poone")
    assert await sync.process_pending() == 0  # nothing downloaded again

    assert mock_tg.download_file.await_count == 1
    assert retagged == [("Siavash Ghomayshi - Golaye Pooneh.mp3",
                         {"title": "Golaye Poone", "artist": "Siavash Ghomayshi"})]
    assert (library_dir / "Siavash Ghomayshi - Golaye Poone.mp3").exists()
    assert not (library_dir / "Siavash Ghomayshi - Golaye Pooneh.mp3").exists()
    manifest = await sync_state_repo.get_json(MANIFEST_KEY)
    assert manifest["10"]["file"] == "Siavash Ghomayshi - Golaye Poone.mp3"


async def test_unreadable_file_on_retag_is_rebuilt(
    track_repo, sync_state_repo, mock_tg, mock_dl, library_dir: Path, monkeypatch
) -> None:
    t = await add_channel_track(track_repo, 10, "Old", artist="A", tg_file_id="f1")
    sync = make_sync(track_repo, sync_state_repo, mock_tg, mock_dl, library_dir)
    await sync.process_pending()

    def broken(p, **kw):
        raise ValueError("can't sync to MPEG frame")

    monkeypatch.setattr(audio, "retag", broken)
    await track_repo.update_track(t.id, title="New")
    assert await sync.process_pending() == 1  # rebuilt from source
    assert mock_tg.download_file.await_count == 2
    assert (library_dir / "A - New.mp3").exists()
    assert not (library_dir / "A - Old.mp3").exists()
