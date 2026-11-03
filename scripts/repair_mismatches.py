"""One-off reconciliation of the Spotify / YouTube playlists with the verified
matcher.

Why this exists: before the matching fix, yt->sp tracks carried no duration and
the matcher accepted \"title OR duration\", so the top search hit could be a
cover, another artist's same-title song, or a shortened video. This script
re-checks every recent mapping against its *source* (Telegram ID3 / YouTube
video / Spotify row) and:

  * keeps a mapping that the current matcher validates,
  * replaces a wrong mapping with the best validated match (if one exists),
  * otherwise removes the wrong entry and marks the row failed.

Dry-run by default. Run inside the navaar pod:

    python /tmp/repair_mismatches.py --limit-rows 160
    python /tmp/repair_mismatches.py --limit-rows 160 --apply

The DB is updated *before* the playlist so the opposite pull loop sees the row
and skips re-importing the change.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
from datetime import UTC, datetime

import structlog
from telegram import Bot

from navaar.config import Settings
from navaar.db.engine import get_session_factory, init_db
from navaar.db.repository import SyncLogRepository, SyncStateRepository, TrackRepository
from navaar.matching import score_candidate
from navaar.spotify.client import SpotifyClient
from navaar.telegram.cards import TrackCardService
from navaar.ytmusic.client import YTMusicClient

logger = structlog.get_logger()

SP_SNAPSHOT = "sp_playlist_snapshot"
YT_SNAPSHOT = "yt_playlist_snapshot"


def _sp_seconds(track: dict) -> int | None:
    ms = track.get("duration_ms")
    return round(ms / 1000) if ms else None


def _valid(source_artist, source_title, source_seconds, candidate) -> bool:
    if not candidate:
        return False
    if "id" in candidate:  # Spotify
        return score_candidate(
            source_title,
            source_seconds,
            candidate.get("name"),
            _sp_seconds(candidate),
            source_artist=source_artist,
            candidate_artists=candidate.get("artists"),
        ) is not None
    return score_candidate(  # YouTube
        source_title,
        source_seconds,
        candidate.get("title"),
        candidate.get("duration_seconds"),
        source_artist=source_artist,
        candidate_artists=[a.get("name") for a in candidate.get("artists", [])],
    ) is not None


async def _refresh_cards(cards: TrackCardService | None, ids: set[int]) -> None:
    if cards is None:
        return
    for track_id in sorted(ids):
        with contextlib.suppress(Exception):
            await cards.refresh(track_id)


async def main(apply: bool, limit_rows: int) -> None:
    settings = Settings()
    await init_db(settings.database_url)
    sf = get_session_factory()
    tracks = TrackRepository(sf)
    state = SyncStateRepository(sf)
    sync_log = SyncLogRepository(sf)

    yt = YTMusicClient(
        auth_file=settings.ytmusic_auth_file,
        playlist_id=settings.ytmusic_playlist_id,
        client_id=settings.ytmusic_client_id,
        client_secret=settings.ytmusic_client_secret,
    )
    sp = SpotifyClient(
        client_id=settings.spotify_client_id,
        client_secret=settings.spotify_client_secret,
        redirect_uri=settings.spotify_redirect_uri,
        playlist_id=settings.spotify_playlist_id,
        cache_path=settings.spotify_cache_path,
    )
    cards: TrackCardService | None = None
    if settings.track_cards_enabled:
        cards = TrackCardService(
            Bot(settings.telegram_bot_token),
            settings.telegram_channel_id,
            tracks,
            sp_enabled=True,
            enabled=True,
        )

    sp_tracks = await asyncio.to_thread(sp.get_playlist_tracks)
    sp_by = {t["id"]: t for t in sp_tracks}
    yt_tracks = await asyncio.to_thread(yt.get_playlist_tracks)
    yt_by = {t["videoId"]: t for t in yt_tracks}

    rows = await tracks.get_recent_tracks(limit=limit_rows)
    all_rows = await tracks.get_all_tracks(limit=5000)
    active_statuses = {
        "synced", "duplicate", "pending", "retry_scheduled",
        "identifying", "searching", "syncing",
    }
    by_id = {r.id: r for r in rows}
    by_file = {r.tg_file_id: r for r in rows if r.tg_file_id and r.direction == "tg_to_sp"}

    # ---------- Spotify reconciliation ----------
    sp_plan: dict[int, str | None] = {}
    sp_source: dict[int, tuple[str | None, str | None, int | None]] = {}
    yt_duration_updates: dict[int, int] = {}
    for row in rows:
        if row.direction not in ("tg_to_sp", "yt_to_sp"):
            continue
        if row.direction == "yt_to_sp":
            video = yt_by.get(row.yt_video_id)
            if not video:
                continue
            source = (
                (video.get("artists") or [{}])[0].get("name"),
                video.get("title"),
                video.get("duration_seconds"),
            )
            if row.duration_seconds in (None, 0) and source[2]:
                yt_duration_updates[row.id] = source[2]
        else:
            source = (row.artist, row.title, row.duration_seconds)
        sp_source[row.id] = source

        # Unsynced = deliberately removed. Failed/pending are not in the playlist yet.
        if row.status not in ("synced", "duplicate"):
            continue

        current = sp_by.get(row.sp_track_id) if row.sp_track_id else None
        if _valid(*source, current):
            sp_plan[row.id] = row.sp_track_id
            continue
        match = await asyncio.to_thread(sp.find_best_match, *source)
        sp_plan[row.id] = match["id"] if match else None
        logger.info(
            "repair_sp_decision",
            track_id=row.id,
            source=f"{source[0]} — {source[1]}",
            old=row.sp_track_id,
            old_name=(current or {}).get("name"),
            new=sp_plan[row.id],
            new_name=(match or {}).get("name"),
        )

    # Anchor ids that must stay because a Spotify-sourced row owns them.
    anchors = {
        row.sp_track_id
        for row in rows
        if row.direction in ("sp_to_tg", "sp_to_yt")
        and row.status in ("synced", "duplicate")
        and row.sp_track_id
    }
    desired_sp = {v for v in sp_plan.values() if v} | anchors
    managed_sp = {row.sp_track_id for row in rows if row.sp_track_id}
    # Never remove an id that a live row outside this window still owns.
    protected_sp = {
        row.sp_track_id
        for row in all_rows
        if row.id not in sp_plan and row.status in active_statuses and row.sp_track_id
    }
    playlist_sp = set(sp_by)
    add_sp = sorted(desired_sp - playlist_sp)
    remove_sp = sorted((managed_sp & playlist_sp) - desired_sp - protected_sp)

    print(f"\n[Spotify] rows={len(sp_plan)} add={len(add_sp)} remove={len(remove_sp)}")
    for spid in add_sp:
        print(f"  + {spid}")
    for spid in remove_sp:
        print(f"  - {spid}  {sp_by[spid]['name']} — {sp_by[spid]['artists']}")

    # ---------- YouTube reconciliation (targets only) ----------
    yt_plan: dict[int, str | None] = {}
    yt_source: dict[int, tuple[str | None, str | None, int | None]] = {}
    for row in rows:
        if row.direction not in ("tg_to_yt", "sp_to_yt"):
            continue
        if row.status not in ("synced", "duplicate"):
            continue
        source = (row.artist, row.title, row.duration_seconds)
        yt_source[row.id] = source
        current = yt_by.get(row.yt_video_id) if row.yt_video_id else None
        if _valid(*source, current):
            yt_plan[row.id] = row.yt_video_id
            continue
        match = await asyncio.to_thread(yt.find_best_match, *source)
        if not match:
            # A sibling Spotify row often knows the canonical spelling ("Fajaeh"
            # vs "Faje-eh"), which rescues a YouTube search the raw title misses.
            sibling = by_file.get(row.tg_file_id)
            spid = sp_plan.get(sibling.id) if sibling else None
            sp_name = (sp_by.get(spid) or {}).get("name") if spid else None
            if sp_name and sp_name.casefold() != (source[1] or "").casefold():
                match = await asyncio.to_thread(
                    yt.find_best_match, source[0], sp_name, source[2]
                )
        yt_plan[row.id] = match["videoId"] if match else None
        logger.info(
            "repair_yt_decision",
            track_id=row.id,
            source=f"{source[0]} — {source[1]}",
            old=row.yt_video_id,
            old_title=(current or {}).get("title"),
            new=yt_plan[row.id],
            new_title=(match or {}).get("title"),
        )

    managed_yt = {row.yt_video_id for row in rows if row.yt_video_id}
    protected_yt = {
        row.yt_video_id
        for row in all_rows
        if row.id not in yt_plan and row.status in active_statuses and row.yt_video_id
    }
    desired_yt = {v for v in yt_plan.values() if v}
    playlist_yt = set(yt_by)
    add_yt = sorted(desired_yt - playlist_yt)
    remove_yt = sorted((managed_yt & playlist_yt) - desired_yt - protected_yt)
    print(f"\n[YouTube] rows={len(yt_plan)} add={len(add_yt)} remove={len(remove_yt)}")
    for vid in add_yt:
        print(f"  + {vid}")
    for vid in remove_yt:
        print(f"  - {vid}  {yt_by[vid].get('title')}")

    if not apply:
        print("\nDRY RUN — pass --apply to write.")
        return

    now = datetime.now(UTC)
    touched: set[int] = set()

    # ----- Spotify: DB first (so sp->tg skips the new id), then playlist -----
    for row_id, spid in sp_plan.items():
        row = by_id[row_id]
        if spid == row.sp_track_id:
            continue
        touched.add(row_id)
        if spid:
            await tracks.mark_synced(row_id, sp_track_id=spid, failure_reason=None)
        else:
            await tracks.update_track(
                row_id, status="failed", failure_reason="no_sp_match", sp_track_id=None
            )
        if spid and spid not in playlist_sp:
            try:
                await asyncio.to_thread(sp.add_to_playlist, spid)
                playlist_sp.add(spid)
            except Exception:
                logger.error("repair_sp_add_failed", track_id=row_id, sp_track_id=spid, exc_info=True)
                await tracks.update_track(
                    row_id, status="failed", failure_reason="no_sp_match", sp_track_id=None
                )
                continue
        await sync_log.log(
            "repair_sp_mapping",
            track_id=row_id,
            direction=row.direction,
            details={"old": row.sp_track_id, "new": spid, "at": now.isoformat()},
        )
    for spid in remove_sp:
        try:
            await asyncio.to_thread(sp.remove_from_playlist, spid)
            playlist_sp.discard(spid)
        except Exception:
            logger.error("repair_sp_remove_failed", sp_track_id=spid, exc_info=True)

    for row_id, seconds in yt_duration_updates.items():
        await tracks.update_track(row_id, duration_seconds=seconds)

    sp_snapshot = await state.get_json(SP_SNAPSHOT)
    if isinstance(sp_snapshot, list):
        next_ids = [i for i in sp_snapshot if i in playlist_sp]
        next_ids += [i for i in add_sp if i in playlist_sp and i not in next_ids]
        await state.set_json(SP_SNAPSHOT, next_ids)

    # ----- YouTube: DB first, then add/remove -----
    for row_id, vid in yt_plan.items():
        row = by_id[row_id]
        if vid == row.yt_video_id:
            continue
        touched.add(row_id)
        old_vid = row.yt_video_id
        if vid is None:
            await tracks.mark_failed(row_id, "no_yt_match")
            continue
        await tracks.mark_synced(row_id, yt_video_id=vid)
        try:
            result = await asyncio.to_thread(yt.add_to_playlist, vid)
            new_set_id = (result or {}).get("id")
            if new_set_id:
                await tracks.update_track(row_id, yt_set_video_id=new_set_id)
            playlist_yt.add(vid)
        except Exception:
            logger.error("repair_yt_add_failed", track_id=row_id, video_id=vid, exc_info=True)
            await tracks.update_track(row_id, status="failed", failure_reason="no_yt_match")
            await tracks.update_track(row_id, yt_video_id=old_vid)
            continue
        await sync_log.log(
            "repair_yt_mapping",
            track_id=row_id,
            direction=row.direction,
            details={"old": old_vid, "new": vid, "at": now.isoformat()},
        )
    for vid in remove_yt:
        try:
            await asyncio.to_thread(yt.remove_from_playlist, vid)
            playlist_yt.discard(vid)
        except Exception:
            logger.error("repair_yt_remove_failed", video_id=vid, exc_info=True)

    yt_snapshot = await state.get_json(YT_SNAPSHOT)
    if isinstance(yt_snapshot, list):
        next_ids = [i for i in yt_snapshot if i in playlist_yt]
        next_ids += [i for i in add_yt if i in playlist_yt and i not in next_ids]
        await state.set_json(YT_SNAPSHOT, next_ids)

    await _refresh_cards(cards, touched)
    print(f"\nApplied. Touched {len(touched)} rows.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit-rows", type=int, default=160)
    args = parser.parse_args()
    asyncio.run(main(args.apply, args.limit_rows))
