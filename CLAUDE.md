# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies
uv sync

# Run the application
uv run python -m navaar

# Run all tests
uv run pytest tests/ -v

# Run a single test file
uv run pytest tests/unit/test_identifier.py -v

# Run a single test by name
uv run pytest tests/unit/test_tg_to_yt.py -v -k "test_process_pending_no_tracks"

# Lint
uv run ruff check src/ tests/

# Docker build & run
docker compose -f deploy/docker-compose.yml build
docker compose -f deploy/docker-compose.yml up
```

## Architecture

Navaar is a fully async Python service that syncs music across three services: a Telegram channel, a YouTube Music playlist, and a Spotify playlist. Six sync directions run concurrently via asyncio. All components are wired together in `__main__.py`.

### Component Wiring

```
__main__.py
├── Settings (pydantic-settings, env prefix NAVAAR_)
├── Database (SQLAlchemy async + aiosqlite)
│   └── 3 Repositories: TrackRepository, SyncStateRepository, SyncLogRepository
├── External Clients
│   ├── YTMusicClient (YouTube Data API v3, OAuth token refresh)
│   ├── YTDownloader (yt-dlp via subprocess)
│   ├── SpotifyClient (spotipy, PKCE or OAuth) — conditional on playlist_id
│   └── TelegramClient (python-telegram-bot)
├── Sync Layer (dict-based, N-direction)
│   ├── TgToYtSync  → process_pending()      (push)
│   ├── YtToTgSync  → process_new_tracks()   (pull, snapshot diff)
│   ├── TgToSpSync  → process_pending()      (push)     ← conditional
│   ├── SpToTgSync  → process_new_tracks()   (pull)     ← conditional
│   ├── YtToSpSync  → process_pending()      (push)     ← conditional
│   ├── SpToYtSync  → process_pending()      (push)     ← conditional
│   └── SyncEngine  → runs N concurrent polling loops
├── NavaarBot (telegram command handlers + channel_post listener)
└── FastAPI Server (/healthz, /metrics, /api/*)
```

Startup order matters: DB → repositories → clients → sync modules → bot (then inject engine via `set_sync_engine()`) → API server. The bot and engine reference each other, so the engine is injected after bot construction.

Spotify is **enabled only when `NAVAAR_SPOTIFY_PLAYLIST_ID` is set**. Without it, only TG↔YT runs (2 directions). With it, all 6 directions are active.

### Sync Engine Model

`SyncEngine` accepts `sync_modules: dict[str, object]` and `intervals: dict[str, int]`. It introspects each module for either `process_pending()` (push-based) or `process_new_tracks()` (pull-based) and stores the callable in `_cycle_methods`.

`run()` launches one `_run_loop(direction, interval)` task per direction. Each loop:
1. Runs a cycle via the stored callable
2. Waits via `asyncio.wait(FIRST_COMPLETED)` on three events: shutdown, force-sync, or sleep timer
3. Loops until shutdown event is set

Force-sync from bot commands interrupts the sleep per-direction. Shutdown via SIGTERM/SIGINT sets the event and cancels tasks.

### Sync Module Patterns

**Push-based** (`process_pending()`): tg_to_yt, tg_to_sp, yt_to_sp, sp_to_yt
- Fetch pending tracks from DB → identify/search → add to target service → mark synced/failed

**Pull-based** (`process_new_tracks()`): yt_to_tg, sp_to_tg
- Phase 1: retry previously failed tracks
- Phase 2: fetch playlist, diff against stored snapshot (in SyncState), process new IDs
- Snapshots stored as JSON in SyncState (`yt_playlist_snapshot`, `sp_playlist_snapshot`)

**Download flows**: sp_to_tg and yt_to_tg both download audio via yt-dlp (YouTube). Spotify has no audio download API, so sp_to_tg searches YouTube for the same track and downloads from there.

### Fan-Out Strategy

When a track arrives from any source, tracks are created for ALL other targets:
- TG channel post → creates `tg_to_yt` + `tg_to_sp` (if SP enabled)
- YT playlist new track → creates `yt_to_tg` + `yt_to_sp` (if SP enabled, via `sp_enabled` flag on YtToTgSync)
- SP playlist new track → creates `sp_to_tg` + `sp_to_yt` (unconditional in SpToTgSync)

Cross-service dedup prevents loops: if a track already exists with the same `yt_video_id` or `sp_track_id` in a synced state, the fan-out skips creating duplicates.

### Spotify Client

`SpotifyClient` supports two auth modes:
- **PKCE** (default): No client_secret needed. Uses public client_id `5c098bcc800e45d49e476265bc9b6934`. Works on free Spotify accounts.
- **OAuth**: Requires own client_id + client_secret (needs Premium for developer app creation).

Mode is selected automatically: if `client_secret` is provided and non-empty, uses OAuth; otherwise PKCE. All methods are synchronous (matching YTMusicClient pattern). Token cache stored in `.spotify_cache`.

### Track Identification Pipeline

`identifier.py` runs three stages in order: ID3 tags (mutagen) → Telegram audio metadata → filename parsing. First success wins. The filename parser splits on `" - "` (dash/emdash/endash) and strips common suffixes like `(Official Video)`.

### Repository Pattern

All three repositories take `async_sessionmaker` and create a new session per method call. No shared transactions. `TrackRepository` has convenience methods (`mark_synced`, `mark_failed`, `mark_duplicate`, `reset_for_retry`) that wrap `update_track()`. Key Spotify additions: `get_track_by_sp_track_id()` (uses `.limit(1)` because fan-out creates multiple tracks with same sp_track_id across directions) and `get_track_by_tg_file_id_and_direction()` for fan-out dedup.

### Metrics

All Prometheus metrics are defined in `metrics.py` and pre-initialized with all label combinations via `init_metrics()` at startup. Key labels: `direction` (all 6), `error_type`, `method`, `result`. Gauges (pending/failed/synced counts, success rate) are refreshed every cycle in `_update_gauges()`. Spotify-specific: `SP_SEARCH_TOTAL` counter and `SP_SEARCH_DURATION` histogram.

## Key Conventions

- **Direction strings**: `"tg_to_yt"`, `"yt_to_tg"`, `"tg_to_sp"`, `"sp_to_tg"`, `"yt_to_sp"`, `"sp_to_yt"` (plain strings, not enums)
- **Status strings**: `pending` → `identifying` → `searching` → `syncing` → `synced` / `failed` / `duplicate`
- **All modules** use `from __future__ import annotations`
- **Logging**: `structlog.get_logger()` per module, snake_case event names, JSON in production / pretty in TTY
- **Retries**: tenacity decorators on external API calls (3 attempts, exponential backoff). Exception: `send_audio` has no retry (timeout likely means upload succeeded)
- **Cleanup**: downloaded files use `tempfile.mkdtemp(prefix="navaar_")`, cleaned in `finally` blocks
- **Bot commands**: all gated by `_is_admin(update)` check against configured user IDs
- **DB migrations**: `engine.py` runs `_run_migrations()` after `create_all` to handle schema changes on existing SQLite DBs (e.g., adding `sp_track_id` column via ALTER TABLE)

## Testing

- `pytest-asyncio` with `asyncio_mode = "auto"` in pyproject.toml
- Fixtures in `conftest.py` provide in-memory SQLite session factory, three repositories, `mock_sp_client`
- External clients are mocked with `MagicMock` (sync methods) and `AsyncMock` (async methods)
- When mocking sync modules for the engine, use `spec=["process_pending"]` or `spec=["process_new_tracks"]` — plain `MagicMock` returns True for any `hasattr`, which breaks the engine's introspection logic
- Unit tests cover: identifier pipeline, repository CRUD/aggregations, all 6 sync directions
- Integration test covers the full sync engine orchestration

## Deployment

CI/CD: push to main → GitHub Actions (test + build Docker image → GHCR) → ArgoCD auto-syncs `deploy/k8s/` to k3s cluster. Secrets are managed manually in-cluster (ArgoCD ignores Secret data diffs via `ignoreDifferences`). The deployment uses `Recreate` strategy (SQLite, single writer) and node affinity to avoid the control-plane node.

The `.spotify_cache` file must be copied to the PVC's `/data` directory. Generate it locally with `scripts/spotify_auth.py`, then `kubectl cp` to the pod.
