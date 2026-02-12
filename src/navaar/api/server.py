from __future__ import annotations

import json
import time

from fastapi import FastAPI, Query
from fastapi.responses import PlainTextResponse
from prometheus_client import generate_latest

from navaar.db.repository import SyncLogRepository, SyncStateRepository, TrackRepository
from navaar.metrics import UP, UPTIME_SECONDS


def create_app(
    track_repo: TrackRepository | None = None,
    sync_state: SyncStateRepository | None = None,
    sync_log: SyncLogRepository | None = None,
    start_time: float | None = None,
) -> FastAPI:
    app = FastAPI(title="Navaar API", docs_url="/docs", redoc_url=None)
    _start = start_time or time.time()

    # ── Health / Metrics ──────────────────────────────────────────────

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> dict:
        if not track_repo:
            return {"status": "error", "reason": "no_db"}
        return {"status": "ok"}

    @app.get("/metrics")
    async def metrics() -> PlainTextResponse:
        UP.set(1)
        UPTIME_SECONDS.set(round(time.time() - _start, 1))
        return PlainTextResponse(
            generate_latest().decode(),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    # ── JSON API ──────────────────────────────────────────────────────

    def _track_to_dict(t) -> dict:
        return {
            "id": t.id,
            "direction": t.direction,
            "status": t.status,
            "artist": t.artist,
            "title": t.title,
            "identification_method": t.identification_method,
            "tg_message_id": t.tg_message_id,
            "tg_file_id": t.tg_file_id,
            "tg_file_unique_id": t.tg_file_unique_id,
            "yt_video_id": t.yt_video_id,
            "yt_set_video_id": t.yt_set_video_id,
            "duration_seconds": t.duration_seconds,
            "failure_reason": t.failure_reason,
            "retry_count": t.retry_count,
            "max_retries": t.max_retries,
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "updated_at": t.updated_at.isoformat() if t.updated_at else None,
            "synced_at": t.synced_at.isoformat() if t.synced_at else None,
        }

    def _log_to_dict(entry) -> dict:
        return {
            "id": entry.id,
            "track_id": entry.track_id,
            "event": entry.event,
            "direction": entry.direction,
            "details": json.loads(entry.details) if entry.details else None,
            "created_at": entry.created_at.isoformat() if entry.created_at else None,
        }

    @app.get("/api/stats")
    async def api_stats() -> dict:
        if not track_repo:
            return {"error": "no_db"}
        stats = await track_repo.get_stats()
        uptime = round(time.time() - _start, 1)
        # Add sync timestamps
        last_tg = None
        last_yt = None
        if sync_state:
            last_tg = await sync_state.get("last_tg_to_yt_sync")
            last_yt = await sync_state.get("last_yt_to_tg_sync")
        return {
            **stats,
            "uptime_seconds": uptime,
            "last_tg_to_yt_sync": float(last_tg) if last_tg else None,
            "last_yt_to_tg_sync": float(last_yt) if last_yt else None,
        }

    @app.get("/api/counts")
    async def api_counts() -> dict:
        if not track_repo:
            return {"error": "no_db"}
        return await track_repo.get_counts()

    @app.get("/api/tracks")
    async def api_tracks(
        direction: str | None = Query(None, description="tg_to_yt or yt_to_tg"),
        status: str | None = Query(None, description="Filter by status"),
        limit: int = Query(50, ge=1, le=500),
    ) -> dict:
        if not track_repo:
            return {"error": "no_db"}
        tracks = await track_repo.get_recent_tracks(limit=limit, direction=direction)
        if status:
            tracks = [t for t in tracks if t.status == status]
        return {"tracks": [_track_to_dict(t) for t in tracks]}

    @app.get("/api/tracks/{track_id}")
    async def api_track_detail(track_id: int) -> dict:
        if not track_repo:
            return {"error": "no_db"}
        t = await track_repo.get_track(track_id)
        if not t:
            return {"error": "not_found"}
        result = _track_to_dict(t)
        if sync_log:
            logs = await sync_log.get_logs_for_track(track_id, limit=20)
            result["logs"] = [_log_to_dict(entry) for entry in logs]
        return result

    @app.get("/api/failed")
    async def api_failed(
        direction: str | None = Query(None),
    ) -> dict:
        if not track_repo:
            return {"error": "no_db"}
        failed = await track_repo.get_failed_tracks(direction)
        return {
            "count": len(failed),
            "tracks": [_track_to_dict(t) for t in failed],
        }

    @app.get("/api/pending")
    async def api_pending() -> dict:
        if not track_repo:
            return {"error": "no_db"}
        tg = await track_repo.get_pending_tracks("tg_to_yt")
        yt = await track_repo.get_pending_tracks("yt_to_tg")
        return {
            "count": len(tg) + len(yt),
            "tg_to_yt": [_track_to_dict(t) for t in tg],
            "yt_to_tg": [_track_to_dict(t) for t in yt],
        }

    @app.get("/api/logs")
    async def api_logs(
        limit: int = Query(50, ge=1, le=500),
        track_id: int | None = Query(None),
    ) -> dict:
        if not sync_log:
            return {"error": "no_db"}
        if track_id:
            logs = await sync_log.get_logs_for_track(track_id, limit=limit)
        else:
            logs = await sync_log.get_recent_logs(limit=limit)
        return {"logs": [_log_to_dict(entry) for entry in logs]}

    @app.get("/api/sync-state")
    async def api_sync_state() -> dict:
        if not sync_state:
            return {"error": "no_db"}
        last_tg = await sync_state.get("last_tg_to_yt_sync")
        last_yt = await sync_state.get("last_yt_to_tg_sync")
        yt_snapshot = await sync_state.get_json("yt_playlist_snapshot")
        return {
            "last_tg_to_yt_sync": float(last_tg) if last_tg else None,
            "last_yt_to_tg_sync": float(last_yt) if last_yt else None,
            "yt_playlist_track_count": len(yt_snapshot) if isinstance(yt_snapshot, list) else 0,
        }

    return app
