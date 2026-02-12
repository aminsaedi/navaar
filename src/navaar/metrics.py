from prometheus_client import Counter, Gauge, Histogram, Info

# ── Info ──────────────────────────────────────────────────────────────

SERVICE_INFO = Info(
    "navaar",
    "Navaar service info",
)

# ── Counters ──────────────────────────────────────────────────────────

SYNC_CYCLES = Counter(
    "navaar_sync_cycles_total",
    "Total sync cycles executed",
    ["direction"],
)
TRACKS_DISCOVERED = Counter(
    "navaar_tracks_discovered_total",
    "Total tracks discovered",
    ["direction"],
)
TRACKS_SYNCED = Counter(
    "navaar_tracks_synced_total",
    "Total tracks successfully synced",
    ["direction"],
)
DUPLICATES_SKIPPED = Counter(
    "navaar_duplicates_skipped_total",
    "Total duplicate tracks skipped",
    ["direction"],
)
SYNC_ERRORS = Counter(
    "navaar_sync_errors_total",
    "Total sync errors",
    ["direction", "error_type"],
)
RETRIES_TOTAL = Counter(
    "navaar_retries_total",
    "Total retry attempts",
    ["direction"],
)
IDENTIFICATION_TOTAL = Counter(
    "navaar_identification_total",
    "Total track identifications by method",
    ["method"],
)
YT_SEARCH_TOTAL = Counter(
    "navaar_yt_search_total",
    "YouTube Music search results",
    ["result"],
)
TG_UPLOAD_TOTAL = Counter(
    "navaar_tg_upload_total",
    "Telegram upload results",
    ["result"],
)
YT_DOWNLOAD_TOTAL = Counter(
    "navaar_yt_download_total",
    "YouTube download results",
    ["result"],
)
TG_DOWNLOAD_TOTAL = Counter(
    "navaar_tg_download_total",
    "Telegram download results",
    ["result"],
)

# ── Gauges ────────────────────────────────────────────────────────────

TRACKS_TOTAL_GAUGE = Gauge(
    "navaar_tracks_total",
    "Total tracks in database",
)
TRACKS_PENDING_GAUGE = Gauge(
    "navaar_tracks_pending",
    "Currently pending tracks",
    ["direction"],
)
TRACKS_FAILED_GAUGE = Gauge(
    "navaar_tracks_failed",
    "Currently failed tracks",
    ["direction"],
)
TRACKS_SYNCED_GAUGE = Gauge(
    "navaar_tracks_synced_current",
    "Current synced tracks count",
    ["direction"],
)
TRACKS_DUPLICATE_GAUGE = Gauge(
    "navaar_tracks_duplicate",
    "Current duplicate tracks count",
    ["direction"],
)
LAST_SYNC_TIMESTAMP = Gauge(
    "navaar_last_sync_timestamp_seconds",
    "Timestamp of last successful sync cycle",
    ["direction"],
)
LAST_SYNC_DURATION = Gauge(
    "navaar_last_sync_duration_seconds",
    "Duration of the most recent sync cycle",
    ["direction"],
)
LAST_SYNC_PROCESSED = Gauge(
    "navaar_last_sync_processed_tracks",
    "Number of tracks processed in last sync cycle",
    ["direction"],
)
UP = Gauge(
    "navaar_up",
    "Whether the service is up",
)
UPTIME_SECONDS = Gauge(
    "navaar_uptime_seconds",
    "Service uptime in seconds",
)
SUCCESS_RATE = Gauge(
    "navaar_success_rate_percent",
    "Overall sync success rate",
)

# ── Histograms ────────────────────────────────────────────────────────

SYNC_CYCLE_DURATION = Histogram(
    "navaar_sync_cycle_duration_seconds",
    "Duration of sync cycles",
    ["direction"],
    buckets=(1, 5, 10, 30, 60, 120, 300),
)
TRACK_SYNC_DURATION = Histogram(
    "navaar_track_sync_duration_seconds",
    "Duration of individual track sync",
    ["direction"],
    buckets=(1, 5, 10, 30, 60, 120),
)
YT_SEARCH_DURATION = Histogram(
    "navaar_yt_search_duration_seconds",
    "Duration of YouTube Music searches",
    buckets=(0.5, 1, 2, 5, 10),
)


# ── Initialization ───────────────────────────────────────────────────

def init_metrics(version: str = "0.1.0", playlist_id: str = "") -> None:
    """Pre-initialize all label combinations so they appear in /metrics from startup."""
    SERVICE_INFO.info({"version": version, "playlist_id": playlist_id})

    for direction in ("tg_to_yt", "yt_to_tg"):
        SYNC_CYCLES.labels(direction=direction)
        TRACKS_DISCOVERED.labels(direction=direction)
        TRACKS_SYNCED.labels(direction=direction)
        DUPLICATES_SKIPPED.labels(direction=direction)
        RETRIES_TOTAL.labels(direction=direction)
        TRACKS_PENDING_GAUGE.labels(direction=direction).set(0)
        TRACKS_FAILED_GAUGE.labels(direction=direction).set(0)
        TRACKS_SYNCED_GAUGE.labels(direction=direction).set(0)
        TRACKS_DUPLICATE_GAUGE.labels(direction=direction).set(0)
        LAST_SYNC_TIMESTAMP.labels(direction=direction).set(0)
        LAST_SYNC_DURATION.labels(direction=direction).set(0)
        LAST_SYNC_PROCESSED.labels(direction=direction).set(0)
        SYNC_CYCLE_DURATION.labels(direction=direction)
        TRACK_SYNC_DURATION.labels(direction=direction)

    for error_type in ("no_yt_match", "unexpected", "cycle_crash",
                        "sync_failed", "retry_failed", "download_failed", "upload_failed"):
        for direction in ("tg_to_yt", "yt_to_tg"):
            SYNC_ERRORS.labels(direction=direction, error_type=error_type)

    for method in ("id3", "tg_metadata", "filename"):
        IDENTIFICATION_TOTAL.labels(method=method)

    for result in ("found", "not_found"):
        YT_SEARCH_TOTAL.labels(result=result)

    for result in ("success", "failure"):
        TG_UPLOAD_TOTAL.labels(result=result)
        YT_DOWNLOAD_TOTAL.labels(result=result)
        TG_DOWNLOAD_TOTAL.labels(result=result)
