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

The six sync modules are thin subclasses over two shared base classes. The concrete
modules (e.g. `tg_to_yt.py`) are ~20 lines: they set class attributes and wire clients.

**Push-based** (`process_pending()`): tg_to_yt, tg_to_sp, yt_to_sp, sp_to_yt
- `BasePushSync` (`sync/_base_push.py`) holds the whole flow. Per-service differences
  live in a `TargetAdapter` (`sync/_targets.py`: `YT_TARGET`/`SP_TARGET`) — match id/name
  keys, db field, no-match reason, search metrics. Subclasses set `direction`, `target`,
  and `identify_from_telegram` (tg_* download+identify the audio first).
- Fetch pending tracks from DB → identify/search → add to target service → mark synced/failed

**Pull-based** (`process_new_tracks()`): yt_to_tg, sp_to_tg
- `BasePullSync` (`sync/_base_pull.py`) holds the retry-then-snapshot-diff skeleton plus the
  shared `_download_and_upload()` (yt-dlp download → Telegram upload → mark synced). Subclasses
  implement `_retry_track`/`_sync_new` and set `snapshot_key`/`id_key`/`id_field`.
- Phase 1: retry previously failed tracks
- Phase 2: fetch playlist, diff against stored snapshot (in SyncState), process new IDs
- Snapshots stored as JSON in SyncState (`yt_playlist_snapshot`, `sp_playlist_snapshot`)

**Blocking clients**: spotipy/ytmusic methods are synchronous; the base classes call them via
`asyncio.to_thread(...)` so a slow/backed-off external call can't stall the event loop (and the
other five loops, the bot, and `/healthz`).

**Download flows**: sp_to_tg and yt_to_tg both download audio via yt-dlp (YouTube). Spotify has no audio download API, so sp_to_tg searches YouTube for the same track and downloads from there. yt-dlp is invoked as `python -m yt_dlp` (via `sys.executable`) so it resolves regardless of PATH. `YTDownloader` re-encodes any file larger than `NAVAAR_TELEGRAM_MAX_UPLOAD_MB` (default 50, the Bot API limit) to a lower mp3 bitrate that fits, computed from the track duration via ffprobe — so long tracks still sync.

### Fan-Out Strategy

When a track arrives from any source, tracks are created for ALL other targets. The
secondary-target creation is centralized in `FanOut` (`sync/fanout.py`) with one consistent
dedup rule (`TrackRepository.has_track_for_direction`); the caller creates the primary/source
track (already deduped) itself:
- TG channel post → bot creates `tg_to_yt`, then `FanOut.from_telegram` → `tg_to_sp` (if SP enabled)
- YT playlist new track → `yt_to_tg` created, then `FanOut.from_youtube` → `yt_to_sp` (if SP enabled)
- SP playlist new track → `sp_to_tg` created, then `FanOut.from_spotify` → `sp_to_yt`

Cross-service dedup prevents loops: `has_track_for_direction` checks whether a track already
exists for the target direction keyed by the same external id before creating a fan-out row.

### Status Cards

`TrackCardService` (`telegram/cards.py`) replies, in the channel, to each track's audio
message with a live "status card": where it was first seen (TG/YT/SP), per-platform sync
status, and inline URL buttons to the YT Music / Spotify entries once they exist.

- A *logical track* is the set of `Track` rows sharing the origin's external id
  (`TrackRepository.get_sibling_tracks` keys off the direction's source prefix). One card
  per logical track; its `tg_message_id` (the card reply) is stamped onto every sibling row
  via `set_card_message_id`, so any direction can find and edit the same card.
- The card is posted once (the bot on a channel post, or `_download_and_upload` once the
  TG upload creates the anchor message) and **edited in place** thereafter. Both sync base
  classes call `_emit_card(track_id)` after every terminal state, so all six directions
  refresh the same card. A per-logical-track `asyncio.Lock` prevents two concurrent loops
  double-posting the first card.
- Best-effort: `refresh()` swallows all its own exceptions and an "is not modified" edit is
  a no-op — a card failure can never break a sync.
- `/card [id]` (admin) posts/refreshes a card on demand (defaults to the most recent track)
  — used to backfill. Gated by `NAVAAR_TRACK_CARDS_ENABLED` (default on).

### Conversational Control

`NavaarAgent` (`telegram/agent.py`) lets you manage Navaar in natural language: reply to a
track's audio message or status card in the channel and @-mention the bot ("unsync this from
spotify"), or DM the bot (admin-gated; "how many failed tracks are there?").

- It's a **real agentic coder**: the **Claude Agent SDK** (`claude-agent-sdk`) runs Claude
  Code *inside the pod*. `NavaarAgent.run()` calls `query()` with
  `permission_mode="bypassPermissions"`, `cwd`/`HOME` = `nl_workspace_dir` (`/data/agent`),
  `max_turns`, and `setting_sources=[]`; it reads `ResultMessage.result` for the final reply.
  The model backend is the **Anthropic-style** endpoint (`ANTHROPIC_BASE_URL` +
  `ANTHROPIC_API_KEY`, read from the env). The image installs the `claude` CLI
  (`npm i -g @anthropic-ai/claude-code`; Node is already present for yt-dlp).
- The agent has Claude Code's built-in tools — **Bash, Read, Write, Edit, Glob, Grep** — so it
  does dynamic work (e.g. "find duplicates") by writing and running its own Python over
  `/data/navaar.db`. There is no hardcoded analysis tool by design.
- Reliable mutations are an **in-process MCP server** (`create_sdk_mcp_server("navaar", …)`)
  exposing `status`, `unsync`, `resync`, `delete`, `delete_message` (reusing the YT/Spotify
  OAuth clients + card refresh). The agent calls `mcp__navaar__*` or uses Bash — its choice.
  The system prompt documents the DB schema and **mandates honesty** about scope (it only sees
  tracks Navaar ingested; a bot can't read older channel history — no channel-wide claims).
- **Conversation memory**: one shared session across the channel and all DMs. `run()` passes
  `resume=<session_id>` so messages accumulate into a single conversation; the id is persisted
  in `SyncState` (`agent_session_id`) and the transcript on the `/data/agent` PVC, so memory
  survives restarts. Managed by three admin slash commands (DM, or channel via `@bot /cmd`):
  `/context` (token/turn/cost readout + % of the window), `/compact` (summarize → delete →
  reseed a fresh, smaller session), `/reset` (`delete_session` + clear). Claude Code's
  autocompact is also on. A stale/missing resumed session is auto-cleared so it can't wedge
  the bot.
- **Security**: this is Bash-in-the-pod as uid 1000 next to the DB and the YT/SP/bot tokens,
  driven by Telegram messages (track titles are attacker-influenceable). `bypassPermissions`
  approves everything. Bounded by an enable flag, `max_turns`, an overall `wait_for(timeout)`,
  a single-flight `asyncio.Lock`, and the channel-mention / admin-DM gates. Accepted tradeoff.
- Target resolution: `get_logical_track_by_message_id` (matches `tg_message_id` or
  `card_message_id`) + `get_sibling_tracks`, passed to the agent as prompt context. Channel
  gate = posting rights (channel posts have no `from_user`); DM gate = `_is_admin`. Config:
  `NAVAAR_NL_AGENT_ENABLED`, `NAVAAR_NL_MODEL`, `NAVAAR_NL_MAX_TURNS`,
  `NAVAAR_NL_REQUEST_TIMEOUT`, `NAVAAR_NL_WORKSPACE_DIR`, plus `ANTHROPIC_BASE_URL` /
  `ANTHROPIC_API_KEY` / `HOME` in the configmap.

### Resilience & Alerting

- **Auth errors** (`auth_errors.py`): permanent failures (401/403/`invalid_grant`/revoked) are
  classified centrally. Tenacity uses `retry_if_transient` so the clients don't waste attempts
  retrying a revoked token. The engine's crash handler classifies the service, increments
  `AUTH_ERRORS{service}`, and tags the error `auth_error` (vs `cycle_crash`).
- **Engine backoff/circuit breaker**: `SyncEngine` tracks consecutive failures per direction,
  applies exponential backoff to the next sleep (capped by `backoff_max_seconds`), and sets
  `DIRECTION_HEALTH{direction}=0` after `circuit_open_after` crashes. One bad credential degrades
  only its directions; the rest keep running.
- **Telegram alerts** (`telegram/alerts.py`): `AlertNotifier` DMs the admin/alert chat on systemic
  crashes — auth failures escalate on the first crash, generic crashes after `alert_consecutive_crashes`,
  with a cooldown so a crash loop sends one alert (not one per cycle) plus a "recovered" message.
  All methods swallow their own exceptions. Configured via `NAVAAR_ALERT_*` (falls back to the first
  admin id when `alert_chat_id` is unset).
- **Readiness**: `/healthz` is lenient (liveness only). `/readyz` returns 503 `degraded` when a
  direction's `last_{direction}_sync` is older than `interval * readiness_stale_multiplier`, so a
  silent crash-loop flips the pod NotReady instead of staying `1/1 Ready`.
- **Logging**: the prod (JSON) structlog chain includes `format_exc_info`, so `logger.error(..., exc_info=True)`
  emits a real traceback string (no frame locals — avoids leaking tokens).

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

CI/CD: push to main → GitHub Actions (ruff lint + pytest w/ coverage gate, then build Docker image → GHCR) → ArgoCD auto-syncs `deploy/k8s/` to k3s cluster. Secrets are managed manually in-cluster (ArgoCD ignores Secret data diffs via `ignoreDifferences`). The deployment uses `Recreate` strategy (SQLite, single writer) and node affinity to avoid the control-plane node.

The pod runs as non-root (uid 1000); an init-container chowns `/data` to 1000:1000 on start, so `kubectl cp` of files (which land as the local user's uid) no longer needs a manual chown. A `backup-cronjob.yaml` runs a daily SQLite online `.backup` into `/data/backups` (keeps the last 14) on the same node.

The `.spotify_cache` file must be copied to the PVC's `/data` directory. Generate it locally with `scripts/spotify_auth.py` (run via `uv run` so the pinned spotipy is used), then `kubectl cp` to the pod. Spotify's shared public PKCE client periodically has its refresh token revoked — when that happens all SP directions alert via Telegram and `/readyz` goes degraded; re-run the auth script and re-copy the cache.
