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

Navaar is a fully async Python service that bidirectionally syncs music between a Telegram channel and a YouTube Music playlist. All components are wired together in `__main__.py` and run concurrently via asyncio.

### Component Wiring

```
__main__.py
├── Settings (pydantic-settings, env prefix NAVAAR_)
├── Database (SQLAlchemy async + aiosqlite)
│   └── 3 Repositories: TrackRepository, SyncStateRepository, SyncLogRepository
├── External Clients
│   ├── YTMusicClient (ytmusicapi, OAuth token refresh)
│   ├── YTDownloader (yt-dlp via subprocess)
│   └── TelegramClient (python-telegram-bot)
├── Sync Layer
│   ├── TgToYtSync → process_pending() returns count
│   ├── YtToTgSync → process_new_tracks() returns count
│   └── SyncEngine → runs two concurrent polling loops
├── NavaarBot (telegram command handlers + channel_post listener)
└── FastAPI Server (/healthz, /metrics, /api/*)
```

Startup order matters: DB → repositories → clients → sync engines → bot (then inject engine via `set_sync_engine()`) → API server. The bot app and sync engine reference each other, so the engine is injected after bot construction.

### Sync Engine Model

`SyncEngine.run()` launches two concurrent `_run_loop()` tasks (one per direction). Each loop:
1. Runs a cycle (`process_pending()` or `process_new_tracks()`)
2. Waits via `asyncio.wait(FIRST_COMPLETED)` on three events: shutdown, force-sync, or sleep timer
3. Loops until shutdown event is set

Force-sync from bot commands interrupts the sleep. Shutdown via SIGTERM/SIGINT sets the event and cancels tasks gracefully.

### YT→TG Diff Detection

YtToTgSync stores a playlist snapshot (list of video IDs) in SyncState. Each cycle fetches the current playlist, diffs against the snapshot, and processes new IDs. The snapshot is updated after processing.

### Track Identification Pipeline

`identifier.py` runs three stages in order: ID3 tags (mutagen) → Telegram audio metadata → filename parsing. First success wins. The filename parser splits on `" - "` (dash/emdash/endash) and strips common suffixes like `(Official Video)`.

### Repository Pattern

All three repositories take `async_sessionmaker` and create a new session per method call. No shared transactions. `TrackRepository` has convenience methods (`mark_synced`, `mark_failed`, `mark_duplicate`, `reset_for_retry`) that wrap `update_track()`.

### Metrics

All Prometheus metrics are defined in `metrics.py` and pre-initialized with all label combinations via `init_metrics()` at startup. Key labels: `direction` (`tg_to_yt`/`yt_to_tg`), `error_type`, `method`, `result`. Metrics are incremented inline throughout sync modules, clients, and the engine. Gauges (pending/failed/synced counts, success rate) are refreshed every cycle in `_update_gauges()`.

## Key Conventions

- **Direction strings**: always `"tg_to_yt"` or `"yt_to_tg"` (not enums)
- **Status strings**: `pending` → `identifying` → `searching` → `syncing` → `synced` / `failed` / `duplicate`
- **All modules** use `from __future__ import annotations`
- **Logging**: `structlog.get_logger()` per module, snake_case event names, JSON in production / pretty in TTY
- **Retries**: tenacity decorators on external API calls (3 attempts, exponential backoff). Exception: `send_audio` has no retry (timeout likely means upload succeeded)
- **Cleanup**: downloaded files use `tempfile.mkdtemp(prefix="navaar_")`, cleaned in `finally` blocks
- **Bot commands**: all gated by `_is_admin(update)` check against configured user IDs

## Testing

- `pytest-asyncio` with `asyncio_mode = "auto"` in pyproject.toml
- Fixtures in `conftest.py` provide in-memory SQLite session factory and the three repositories
- External clients are mocked with `MagicMock` (sync methods) and `AsyncMock` (async methods)
- Unit tests cover: identifier pipeline, repository CRUD/aggregations, both sync directions
- Integration test covers the full sync engine orchestration

## Deployment

CI/CD: push to main → GitHub Actions (test + build Docker image → GHCR) → ArgoCD auto-syncs `deploy/k8s/` to k3s cluster. Secrets are managed manually in-cluster (ArgoCD ignores Secret data diffs). The deployment uses `Recreate` strategy (SQLite, single writer) and node affinity to avoid the control-plane node.
