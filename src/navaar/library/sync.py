from __future__ import annotations

import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import structlog

from navaar.db.models import Track
from navaar.db.repository import SyncStateRepository, TrackRepository
from navaar.library import audio
from navaar.library.naming import clean_title, dedupe_key, track_stem, unique_file_name
from navaar.metrics import LIBRARY_BYTES_GAUGE, LIBRARY_FAILED_GAUGE, LIBRARY_TRACKS_GAUGE
from navaar.telegram.client import FileTooBigError, TelegramClient
from navaar.ytmusic.downloader import YTDownloader

logger = structlog.get_logger()

MANIFEST_KEY = "library_manifest"
TMP_DIR_NAME = ".navaar-tmp"

# Failed tracks are retried with exponential backoff (5 min, 10, 20, ... capped at
# a day) so one undownloadable song never blocks the rest or hammers YouTube.
_RETRY_BASE_SECONDS = 300
_RETRY_MAX_SECONDS = 86400


def _retry_delay(attempts: int) -> float:
    return min(_RETRY_BASE_SECONDS * 2 ** max(attempts - 1, 0), _RETRY_MAX_SECONDS)


@dataclass(frozen=True)
class LibraryItem:
    """One song that should be in the library, anchored on its channel message."""

    key: str  # str(tg_message_id) — stable across platforms and renames
    title: str
    artist: str | None
    tg_file_id: str | None
    yt_video_id: str | None


class LibrarySync:
    """Mirror every song in the Telegram channel into a local MP3 folder.

    The folder (``library_dir``) is the single source the rest of the music stack
    reads: Navidrome serves it, Music Assistant mounts it, and the host copies it
    onto the car USB stick. Runs as one more SyncEngine loop (``process_pending``),
    so it inherits backoff, alerting, metrics, and /readyz staleness checks.

    State lives in SyncState[``library_manifest``]: ``{key: {"file": name|None,
    "attempts": n, "next_retry": ts, "error": str|None, "source": ...}}``. Files are
    built in a temp dir inside the library and moved into place with ``os.replace``
    so readers never observe a partial file.
    """

    direction = "library"

    def __init__(
        self,
        track_repo: TrackRepository,
        sync_state: SyncStateRepository,
        tg_client: TelegramClient,
        downloader: YTDownloader,
        library_dir: str,
        *,
        bitrate_kbps: int = 320,
        max_per_cycle: int = 10,
        playlist_name: str = "Navaar",
    ) -> None:
        self._tracks = track_repo
        self._state = sync_state
        self._tg = tg_client
        self._dl = downloader
        self._root = Path(library_dir)
        self._bitrate = bitrate_kbps
        self._max_per_cycle = max_per_cycle
        self._playlist_name = playlist_name

    # The engine wiring injects the card service into every module; the library
    # has no channel presence, so this is a deliberate no-op.
    def set_card_service(self, card_service: object) -> None:
        pass

    # ── planning ──────────────────────────────────────────────────────────

    @staticmethod
    def plan(rows: list[Track]) -> list[LibraryItem]:
        """Channel rows (oldest first) -> the songs the library should hold.

        A song posted twice (same YouTube id, or same artist+title) is kept once,
        under its oldest channel message."""
        items: list[LibraryItem] = []
        seen_names: set[str] = set()
        seen_videos: set[str] = set()
        for row in rows:
            if row.tg_message_id is None:
                continue
            title = clean_title(row.artist, row.title)
            name_key = dedupe_key(row.artist, title)
            if name_key in seen_names or (row.yt_video_id and row.yt_video_id in seen_videos):
                continue
            seen_names.add(name_key)
            if row.yt_video_id:
                seen_videos.add(row.yt_video_id)
            items.append(LibraryItem(
                key=str(row.tg_message_id),
                title=title,
                artist=row.artist,
                tg_file_id=row.tg_file_id,
                yt_video_id=row.yt_video_id,
            ))
        return items

    # ── cycle ─────────────────────────────────────────────────────────────

    async def process_pending(self) -> int:
        if not self._root.is_dir():
            # Fail the cycle loudly (engine alerts + backoff) instead of silently
            # creating an empty library somewhere unexpected.
            raise RuntimeError(f"library dir {self._root} does not exist")
        tmp_root = self._root / TMP_DIR_NAME
        shutil.rmtree(tmp_root, ignore_errors=True)  # leftovers from a killed cycle
        tmp_root.mkdir()

        items = self.plan(await self._tracks.get_channel_tracks())
        wanted = {item.key: item for item in items}
        manifest: dict[str, dict] = await self._state.get_json(MANIFEST_KEY) or {}
        changed = False

        # 1. songs that left the channel: delete their files.
        for key in [k for k in manifest if k not in wanted]:
            name = manifest.pop(key).get("file")
            if name:
                (self._root / name).unlink(missing_ok=True)
                logger.info("library_track_removed", key=key, file=name)
            changed = True

        # 2. files whose entry points at a missing file (deleted by hand): re-fetch.
        for entry in manifest.values():
            if entry.get("file") and not (self._root / entry["file"]).is_file():
                entry["file"] = None
                changed = True

        # 3. stray .mp3 files: adopt ours (manifest lost), delete anything else.
        owned = {e["file"] for e in manifest.values() if e.get("file")}
        for path in sorted(self._root.glob("*.mp3")):
            if path.name in owned:
                continue
            key = audio.read_navaar_id(path)
            if key in wanted and not manifest.get(key, {}).get("file"):
                manifest[key] = {"file": path.name, "source": "adopted"}
                owned.add(path.name)
                logger.info("library_file_adopted", key=key, file=path.name)
            else:
                path.unlink(missing_ok=True)
                logger.info("library_stray_file_removed", file=path.name)
            changed = True

        if changed:
            await self._state.set_json(MANIFEST_KEY, manifest)

        # 4. fetch what's missing, oldest first, bounded per cycle.
        now = time.time()
        built = 0
        for item in items:
            entry = manifest.get(item.key, {})
            if entry.get("file") or entry.get("next_retry", 0) > now:
                continue
            if built >= self._max_per_cycle:
                break
            taken = {e["file"] for e in manifest.values() if e.get("file")}
            try:
                name, source = await self._materialize(item, tmp_root, taken)
            except Exception as exc:
                attempts = entry.get("attempts", 0) + 1
                manifest[item.key] = {
                    "file": None,
                    "attempts": attempts,
                    "next_retry": time.time() + _retry_delay(attempts),
                    "error": str(exc)[:300],
                }
                logger.warning(
                    "library_track_failed", key=item.key, title=item.title,
                    attempts=attempts, error=str(exc)[:300],
                )
            else:
                manifest[item.key] = {"file": name, "source": source}
                built += 1
                logger.info("library_track_added", key=item.key, file=name, source=source)
            # Persist after every track so a restart mid-fill loses nothing.
            await self._state.set_json(MANIFEST_KEY, manifest)

        self._write_playlist(items, manifest)
        shutil.rmtree(tmp_root, ignore_errors=True)
        self._update_metrics(items, manifest)
        return built

    # ── one track ─────────────────────────────────────────────────────────

    async def _materialize(
        self, item: LibraryItem, tmp_root: Path, taken: set[str]
    ) -> tuple[str, str]:
        work = Path(tempfile.mkdtemp(dir=tmp_root))
        try:
            src, source = await self._download(item, work)
            cover = await audio.extract_cover(src, work)
            if cover is None and item.yt_video_id:
                cover = await audio.fetch_youtube_cover(item.yt_video_id, work)
            out = work / "out.mp3"
            await audio.build_mp3(
                src, out,
                title=item.title, artist=item.artist, album=self._playlist_name,
                navaar_id=item.key, cover=cover, bitrate_kbps=self._bitrate,
            )
            name = unique_file_name(track_stem(item.artist, item.title), taken)
            os.replace(out, self._root / name)  # same filesystem -> atomic
            return name, source
        finally:
            shutil.rmtree(work, ignore_errors=True)

    async def _download(self, item: LibraryItem, work: Path) -> tuple[Path, str]:
        """Prefer the exact audio posted in the channel; fall back to YouTube for
        bot uploads (no file id stored) and files over the Bot API's 20 MB cap."""
        if item.tg_file_id:
            try:
                return Path(await self._tg.download_file(item.tg_file_id, str(work))), "telegram"
            except FileTooBigError:
                logger.info("library_tg_file_too_big", key=item.key)
                if not item.yt_video_id:
                    raise
        if item.yt_video_id:
            path = Path(await self._dl.download(item.yt_video_id))
            moved = work / path.name
            shutil.move(path, moved)  # out of the shared download dir
            return moved, "youtube"
        raise RuntimeError("no downloadable source (no Telegram file id, no YouTube id)")

    # ── outputs ───────────────────────────────────────────────────────────

    def _write_playlist(self, items: list[LibraryItem], manifest: dict[str, dict]) -> None:
        """Channel-ordered M3U8 (UTF-8, relative paths): Navidrome imports it as a
        playlist and most car players can play it directly."""
        lines = ["#EXTM3U", f"#PLAYLIST:{self._playlist_name}"]
        for item in items:
            name = manifest.get(item.key, {}).get("file")
            if not name:
                continue
            label = f"{item.artist} - {item.title}" if item.artist else item.title
            lines += [f"#EXTINF:-1,{label}", name]
        content = "\n".join(lines) + "\n"
        path = self._root / f"{self._playlist_name}.m3u8"
        if path.is_file() and path.read_text(encoding="utf-8") == content:
            return
        tmp = path.with_suffix(".m3u8.tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)

    def _update_metrics(self, items: list[LibraryItem], manifest: dict[str, dict]) -> None:
        present = [manifest[i.key]["file"] for i in items if manifest.get(i.key, {}).get("file")]
        LIBRARY_TRACKS_GAUGE.set(len(present))
        LIBRARY_FAILED_GAUGE.set(sum(1 for i in items if manifest.get(i.key, {}).get("error")
                                     and not manifest[i.key].get("file")))
        size = 0
        for name in present:
            try:
                size += (self._root / name).stat().st_size
            except OSError:
                pass
        LIBRARY_BYTES_GAUGE.set(size)

    async def status(self) -> dict[str, object]:
        """Summary for the /library bot command."""
        items = self.plan(await self._tracks.get_channel_tracks())
        manifest: dict[str, dict] = await self._state.get_json(MANIFEST_KEY) or {}
        present = [i for i in items if manifest.get(i.key, {}).get("file")]
        failed = [
            (i, manifest[i.key]) for i in items
            if not manifest.get(i.key, {}).get("file") and manifest.get(i.key, {}).get("error")
        ]
        size = 0
        for i in present:
            try:
                size += (self._root / manifest[i.key]["file"]).stat().st_size
            except OSError:
                pass
        return {
            "wanted": len(items),
            "present": len(present),
            "pending": len(items) - len(present) - len(failed),
            "failed": failed,
            "bytes": size,
            "dir": str(self._root),
        }
