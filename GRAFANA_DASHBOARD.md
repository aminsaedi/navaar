# Navaar Grafana Dashboard Specification

## Overview

**Navaar** is a bidirectional music sync service between a Telegram channel and a YouTube Music playlist. This document provides everything needed to build a comprehensive Grafana dashboard for monitoring it.

- **Prometheus endpoint:** `http://navaar:8080/metrics`
- **JSON API base:** `http://navaar:8080/api/`
- **Scrape interval:** 15s recommended
- **Job name:** `navaar`

---

## Data Sources

### 1. Prometheus (primary)
All `navaar_*` metrics are exposed at `/metrics` in standard Prometheus exposition format. This is the primary data source for time-series panels.

### 2. JSON API (supplementary)
REST endpoints at `/api/*` expose structured data from the SQLite database. Use the **Infinity** datasource plugin (or JSON API datasource) for table panels and detail views. All responses are JSON.

---

## Prometheus Metrics Reference

### Info Metric

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `navaar_info` | Info | `version`, `playlist_id` | Service metadata |

### Counters (monotonically increasing)

| Metric | Labels | Description |
|--------|--------|-------------|
| `navaar_sync_cycles_total` | `direction` | Total sync cycles executed |
| `navaar_tracks_discovered_total` | `direction` | Tracks first detected (new audio in TG or new track in YT playlist) |
| `navaar_tracks_synced_total` | `direction` | Tracks successfully synced end-to-end |
| `navaar_duplicates_skipped_total` | `direction` | Duplicate tracks detected and skipped |
| `navaar_sync_errors_total` | `direction`, `error_type` | Errors by type and direction |
| `navaar_retries_total` | `direction` | Manual/automatic retry attempts |
| `navaar_identification_total` | `method` | Track identification results by method (id3, tg_metadata, filename) |
| `navaar_yt_search_total` | `result` | YouTube search outcomes (found, not_found) |
| `navaar_tg_upload_total` | `result` | Telegram upload outcomes (success, failure) |
| `navaar_tg_download_total` | `result` | Telegram download outcomes (success, failure) |
| `navaar_yt_download_total` | `result` | YouTube download outcomes (success, failure) |

**Direction values:** `tg_to_yt`, `yt_to_tg`

**Error types:** `no_yt_match`, `unexpected`, `cycle_crash`, `sync_failed`, `retry_failed`, `download_failed`, `upload_failed`

**Identification methods:** `id3`, `tg_metadata`, `filename`

### Gauges (current state)

| Metric | Labels | Description |
|--------|--------|-------------|
| `navaar_up` | - | 1 if service is running, 0 otherwise |
| `navaar_uptime_seconds` | - | Seconds since service started |
| `navaar_tracks_total` | - | Total track records in database |
| `navaar_tracks_pending` | `direction` | Tracks waiting to be synced |
| `navaar_tracks_failed` | `direction` | Tracks in failed state |
| `navaar_tracks_synced_current` | `direction` | Tracks in synced state |
| `navaar_tracks_duplicate` | `direction` | Tracks marked as duplicate |
| `navaar_success_rate_percent` | - | Overall sync success rate (0-100) |
| `navaar_last_sync_timestamp_seconds` | `direction` | Unix timestamp of last sync cycle |
| `navaar_last_sync_duration_seconds` | `direction` | Duration of the most recent sync cycle |
| `navaar_last_sync_processed_tracks` | `direction` | Tracks processed in the most recent cycle |

### Histograms

| Metric | Labels | Buckets | Description |
|--------|--------|---------|-------------|
| `navaar_sync_cycle_duration_seconds` | `direction` | 1, 5, 10, 30, 60, 120, 300 | Full sync cycle duration |
| `navaar_track_sync_duration_seconds` | `direction` | 1, 5, 10, 30, 60, 120 | Per-track sync duration |
| `navaar_yt_search_duration_seconds` | - | 0.5, 1, 2, 5, 10 | YouTube Music search latency |

---

## JSON API Endpoints

All endpoints return JSON. Base URL: `http://navaar:8080`

### GET /api/stats
Aggregate statistics. Ideal for stat/gauge panels.

```json
{
  "total": 4,
  "synced": 4,
  "failed": 0,
  "duplicates": 0,
  "pending": 0,
  "tg_to_yt_synced": 1,
  "yt_to_tg_synced": 3,
  "success_rate": 100.0,
  "uptime_seconds": 3600.0,
  "last_tg_to_yt_sync": 1770933510.03,
  "last_yt_to_tg_sync": 1770933510.04
}
```

### GET /api/counts
Counts grouped by direction and status.

```json
{
  "tg_to_yt": { "synced": 1, "pending": 0, "failed": 0 },
  "yt_to_tg": { "synced": 3, "pending": 0, "failed": 0 }
}
```

### GET /api/tracks?direction=&status=&limit=50
List tracks with optional filters. Ordered by most recent first.

```json
{
  "tracks": [
    {
      "id": 4,
      "direction": "yt_to_tg",
      "status": "synced",
      "artist": "Siavash Ghomayshi - Topic",
      "title": "Ghoroob",
      "identification_method": "yt_metadata",
      "tg_message_id": 606,
      "tg_file_id": null,
      "tg_file_unique_id": null,
      "yt_video_id": "yA8UB7bhawE",
      "yt_set_video_id": "...",
      "duration_seconds": null,
      "failure_reason": null,
      "retry_count": 0,
      "max_retries": 3,
      "created_at": "2026-02-12T21:46:02",
      "updated_at": "2026-02-12T21:46:20",
      "synced_at": "2026-02-12T21:46:20.380575"
    }
  ]
}
```

### GET /api/tracks/{id}
Single track detail with log history.

Returns the same track object as above plus a `logs` array:
```json
{
  "id": 2,
  "...": "...",
  "logs": [
    {
      "id": 3,
      "track_id": 2,
      "event": "track_synced",
      "direction": "tg_to_yt",
      "details": { "video_id": "GVkAZfS_-Cw", "title": "Agar Mandeh Boodi" },
      "created_at": "2026-02-12T21:32:24"
    }
  ]
}
```

### GET /api/failed?direction=
Failed tracks. Returns `{ "count": N, "tracks": [...] }`.

### GET /api/pending
Pending tracks by direction. Returns `{ "count": N, "tg_to_yt": [...], "yt_to_tg": [...] }`.

### GET /api/logs?limit=50&track_id=
Sync log entries. Returns `{ "logs": [...] }`.

### GET /api/sync-state
Current sync engine state.

```json
{
  "last_tg_to_yt_sync": 1770933510.03,
  "last_yt_to_tg_sync": 1770933510.04,
  "yt_playlist_track_count": 4
}
```

### GET /healthz
Returns `{"status": "ok"}`. For uptime monitoring.

### GET /readyz
Returns `{"status": "ok"}` when DB is connected.

---

## Dashboard Layout

### Row 1: Service Health (collapsed: no, height: 3)

| Panel | Type | Width | Description |
|-------|------|-------|-------------|
| **Service Up** | Stat | 3 | `navaar_up` - Green=1, Red=0. Thresholds: 1=green, 0=red |
| **Uptime** | Stat | 3 | `navaar_uptime_seconds` - Format as duration (d hh:mm:ss) |
| **Success Rate** | Gauge | 3 | `navaar_success_rate_percent` - Thresholds: >90=green, >70=yellow, <=70=red |
| **Total Tracks** | Stat | 3 | `navaar_tracks_total` |
| **Synced** | Stat | 3 | `sum(navaar_tracks_synced_current)` - Green color |
| **Failed** | Stat | 3 | `sum(navaar_tracks_failed)` - Thresholds: 0=green, >0=red |
| **Pending** | Stat | 3 | `sum(navaar_tracks_pending)` - Thresholds: 0=green, >5=yellow, >20=red |
| **Duplicates** | Stat | 3 | `sum(navaar_tracks_duplicate)` - Blue color |

### Row 2: Sync Activity (height: 8)

| Panel | Type | Width | PromQL |
|-------|------|-------|--------|
| **Tracks Synced Rate** | Time series | 12 | `rate(navaar_tracks_synced_total[5m])` by direction. Two series: TG->YT and YT->TG. Stacked area. |
| **Sync Errors Rate** | Time series | 12 | `rate(navaar_sync_errors_total[5m])` by direction, error_type. Line chart with color by error_type. |

### Row 3: Sync Cycles (height: 8)

| Panel | Type | Width | PromQL |
|-------|------|-------|--------|
| **Cycle Duration** | Time series | 8 | `navaar_last_sync_duration_seconds` by direction. Two lines. Y-axis: seconds. |
| **Cycle Duration Heatmap** | Heatmap | 8 | `rate(navaar_sync_cycle_duration_seconds_bucket[5m])` by direction, le. One per direction. |
| **Tracks Processed per Cycle** | Time series | 8 | `navaar_last_sync_processed_tracks` by direction. Bar chart. |

### Row 4: Throughput Breakdown (height: 8)

| Panel | Type | Width | PromQL |
|-------|------|-------|--------|
| **Track Discovery Rate** | Time series | 8 | `rate(navaar_tracks_discovered_total[5m])` by direction |
| **Identification Methods** | Pie chart | 8 | `navaar_identification_total` by method |
| **YouTube Search Results** | Pie chart | 8 | `navaar_yt_search_total` by result |

### Row 5: External Service Health (height: 8)

| Panel | Type | Width | PromQL |
|-------|------|-------|--------|
| **Telegram Uploads** | Time series | 8 | `rate(navaar_tg_upload_total[5m])` by result. success=green, failure=red |
| **Telegram Downloads** | Time series | 8 | `rate(navaar_tg_download_total[5m])` by result |
| **YouTube Downloads** | Time series | 8 | `rate(navaar_yt_download_total[5m])` by result |

### Row 6: Latency (height: 8)

| Panel | Type | Width | PromQL |
|-------|------|-------|--------|
| **Per-Track Sync Duration (p50/p95/p99)** | Time series | 12 | `histogram_quantile(0.5, rate(navaar_track_sync_duration_seconds_bucket[5m]))`, same for 0.95 and 0.99. By direction. |
| **YouTube Search Latency (p50/p95/p99)** | Time series | 12 | `histogram_quantile(0.5, rate(navaar_yt_search_duration_seconds_bucket[5m]))`, same for 0.95, 0.99 |

### Row 7: Current State (height: 8)

| Panel | Type | Width | PromQL |
|-------|------|-------|--------|
| **Tracks by Status (TG->YT)** | Bar gauge | 6 | `navaar_tracks_synced_current{direction="tg_to_yt"}`, `navaar_tracks_failed{direction="tg_to_yt"}`, `navaar_tracks_pending{direction="tg_to_yt"}`, `navaar_tracks_duplicate{direction="tg_to_yt"}` |
| **Tracks by Status (YT->TG)** | Bar gauge | 6 | Same as above with `direction="yt_to_tg"` |
| **Last Sync Age** | Stat | 6 | `time() - navaar_last_sync_timestamp_seconds` by direction. Format as duration. Thresholds: <120s=green, <300s=yellow, >=300s=red |
| **Retries Total** | Stat | 6 | `sum(navaar_retries_total)` |

### Row 8: Recent Tracks Table (height: 10)

| Panel | Type | Width | Data source |
|-------|------|-------|-------------|
| **Recent Tracks** | Table | 24 | JSON API: `/api/tracks?limit=20`. Columns: id, direction, status, artist, title, yt_video_id, created_at, synced_at. Color status column (synced=green, failed=red, pending=yellow). |

### Row 9: Failed Tracks Table (height: 8)

| Panel | Type | Width | Data source |
|-------|------|-------|-------------|
| **Failed Tracks** | Table | 24 | JSON API: `/api/failed`. Columns: id, direction, artist, title, failure_reason, retry_count, created_at. Red header. |

### Row 10: Sync Log (height: 8)

| Panel | Type | Width | Data source |
|-------|------|-------|-------------|
| **Sync Log** | Table | 24 | JSON API: `/api/logs?limit=30`. Columns: id, track_id, event, direction, details, created_at. |

---

## Key PromQL Queries

### Sync rate (tracks/minute)
```promql
rate(navaar_tracks_synced_total[5m]) * 60
```

### Error rate percentage
```promql
rate(navaar_sync_errors_total[5m]) / rate(navaar_sync_cycles_total[5m]) * 100
```

### Time since last sync (useful for alerts)
```promql
time() - navaar_last_sync_timestamp_seconds
```

### Track sync duration percentiles
```promql
# p50
histogram_quantile(0.5, rate(navaar_track_sync_duration_seconds_bucket[5m]))
# p95
histogram_quantile(0.95, rate(navaar_track_sync_duration_seconds_bucket[5m]))
# p99
histogram_quantile(0.99, rate(navaar_track_sync_duration_seconds_bucket[5m]))
```

### YouTube search hit rate
```promql
navaar_yt_search_total{result="found"} / ignoring(result) group_left sum(navaar_yt_search_total) * 100
```

### Telegram upload success rate
```promql
navaar_tg_upload_total{result="success"} / ignoring(result) group_left sum(navaar_tg_upload_total) * 100
```

### Sync cycle throughput
```promql
rate(navaar_sync_cycles_total[5m]) * 60
```

---

## Suggested Alerts

| Alert | PromQL | Severity | Description |
|-------|--------|----------|-------------|
| NavaarDown | `navaar_up == 0` | critical | Service is down |
| NavaarSyncStale | `time() - navaar_last_sync_timestamp_seconds > 300` | warning | No sync cycle in >5 minutes |
| NavaarSyncStaleCritical | `time() - navaar_last_sync_timestamp_seconds > 900` | critical | No sync cycle in >15 minutes |
| NavaarHighFailRate | `sum(navaar_tracks_failed) > 5` | warning | More than 5 failed tracks |
| NavaarPendingBacklog | `sum(navaar_tracks_pending) > 10` | warning | Pending queue growing |
| NavaarHighErrorRate | `rate(navaar_sync_errors_total[5m]) > 0.1` | warning | Errors occurring >6/hr |
| NavaarUploadFailures | `rate(navaar_tg_upload_total{result="failure"}[10m]) > 0` | warning | Telegram uploads failing |
| NavaarDownloadFailures | `rate(navaar_yt_download_total{result="failure"}[10m]) > 0` | warning | YouTube downloads failing |

---

## Dashboard Variables (Template Variables)

| Variable | Type | Values | Description |
|----------|------|--------|-------------|
| `direction` | Custom | `tg_to_yt`, `yt_to_tg` | Filter panels by sync direction |
| `interval` | Interval | `1m`, `5m`, `15m`, `1h` | Rate calculation window |

Use `$direction` in PromQL like: `navaar_tracks_synced_total{direction="$direction"}`

---

## Dashboard Metadata

```json
{
  "title": "Navaar - Music Sync Monitor",
  "uid": "navaar-sync",
  "tags": ["navaar", "music", "sync", "telegram", "youtube"],
  "timezone": "browser",
  "refresh": "30s",
  "time": { "from": "now-6h", "to": "now" }
}
```

---

## Color Scheme

| Element | Color |
|---------|-------|
| TG -> YT direction | `#3B82F6` (blue) |
| YT -> TG direction | `#EF4444` (red) |
| Synced/Success | `#22C55E` (green) |
| Failed/Error | `#EF4444` (red) |
| Pending | `#F59E0B` (amber) |
| Duplicate | `#8B5CF6` (purple) |
| Retries | `#F97316` (orange) |

---

## Notes

- The service runs as a single replica (SQLite-backed), so no aggregation across instances is needed.
- Counters reset on restart. Use `rate()` or `increase()` for meaningful visualizations.
- Gauges (pending, failed, synced) are updated from the DB after every sync cycle, so they're always fresh.
- JSON API endpoints are unauthenticated — ensure network-level access control in production.
- The Infinity datasource plugin is recommended for JSON API panels. Configure with base URL `http://navaar:8080`.
- Histogram buckets are designed around typical operation times: sync cycles can take up to 5 minutes (large downloads), individual track syncs 10-120 seconds.
