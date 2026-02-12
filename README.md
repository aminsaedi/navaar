# Navaar (نوار)

Bidirectional music sync between a Telegram channel and a YouTube Music playlist.

When audio is forwarded to the Telegram channel, Navaar identifies the track and adds it to the YouTube Music playlist. When a track is added to the playlist, Navaar downloads it and posts it to the channel.

## How It Works

**Telegram → YouTube Music**
1. Audio file posted to the Telegram channel
2. Track identified via ID3 tags, Telegram metadata, or filename
3. YouTube Music searched for a match
4. Match added to the configured playlist

**YouTube Music → Telegram**
1. Playlist polled for new tracks
2. New tracks downloaded via yt-dlp
3. Audio uploaded to the Telegram channel with metadata

Both directions run as configurable polling loops (default: 60s TG→YT, 120s YT→TG). Duplicate detection, retry logic with exponential backoff, and full audit logging are built in.

## Setup

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) package manager
- ffmpeg (for audio processing)
- A Telegram bot with admin access to the target channel
- Google OAuth credentials for YouTube Music API

### Install

```bash
git clone https://github.com/aminsaedi/navaar.git
cd navaar
uv sync
```

### YouTube Music OAuth

Generate the OAuth token file:

```bash
uv run python -c "from ytmusicapi import YTMusic; YTMusic.setup_oauth(filepath='oauth.json', open_browser=True)"
```

This opens a browser for Google OAuth consent. Complete the flow and `oauth.json` is created.

### Configuration

Copy the example env file and fill in your values:

```bash
cp .env.example .env
```

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `NAVAAR_TELEGRAM_BOT_TOKEN` | Yes | — | Telegram bot API token |
| `NAVAAR_TELEGRAM_CHANNEL_ID` | No | — | Target Telegram channel ID |
| `NAVAAR_TELEGRAM_ADMIN_USER_IDS` | No | `[]` | JSON list of admin Telegram user IDs |
| `NAVAAR_YTMUSIC_AUTH_FILE` | No | `oauth.json` | Path to YouTube Music OAuth token |
| `NAVAAR_YTMUSIC_PLAYLIST_ID` | No | — | YouTube Music playlist ID |
| `NAVAAR_YTMUSIC_CLIENT_ID` | No | `""` | Google OAuth client ID |
| `NAVAAR_YTMUSIC_CLIENT_SECRET` | No | `""` | Google OAuth client secret |
| `NAVAAR_SYNC_INTERVAL_TG_TO_YT` | No | `60` | Seconds between TG→YT sync cycles |
| `NAVAAR_SYNC_INTERVAL_YT_TO_TG` | No | `120` | Seconds between YT→TG sync cycles |
| `NAVAAR_MAX_RETRIES` | No | `3` | Max retry attempts for failed tracks |
| `NAVAAR_DATABASE_URL` | No | `sqlite+aiosqlite:///navaar.db` | SQLAlchemy database URL |
| `NAVAAR_API_PORT` | No | `8080` | HTTP API port |
| `NAVAAR_LOG_LEVEL` | No | `INFO` | Log level (DEBUG, INFO, WARNING, ERROR) |

### Run

```bash
uv run python -m navaar
```

## Docker

```bash
# Build and run with docker compose
docker compose -f deploy/docker-compose.yml build
docker compose -f deploy/docker-compose.yml up -d
```

Data (SQLite database + OAuth token) persists in the `navaar-data` volume.

## Bot Commands

All commands are restricted to configured admin user IDs.

| Command | Description |
|---------|-------------|
| `/status` | Live sync dashboard with inline action buttons |
| `/stats` | Aggregate statistics with success rate bar |
| `/queue` | Pending tracks waiting to sync |
| `/recent [n]` | Last n synced tracks (default 10) |
| `/track <id>` | Full details for a track with log history |
| `/logs [n]` | Recent sync log entries |
| `/sync [tg\|yt]` | Force immediate sync cycle |
| `/retry <id\|all\|tg\|yt>` | Retry failed tracks |
| `/delete <id>` | Remove a track from the database |
| `/search <query>` | Search YouTube Music |
| `/failed [tg\|yt]` | List failed tracks with reasons |
| `/config` | Show current configuration |
| `/ping` | Check bot responsiveness and uptime |

## API Endpoints

The HTTP API runs on the configured port (default 8080).

| Endpoint | Description |
|----------|-------------|
| `GET /healthz` | Health check |
| `GET /readyz` | Readiness check (verifies DB) |
| `GET /metrics` | Prometheus metrics |
| `GET /api/stats` | Sync statistics (JSON) |
| `GET /api/counts` | Track counts by direction and status |
| `GET /api/tracks` | List tracks with `?direction=`, `?status=`, `?limit=` filters |
| `GET /api/tracks/{id}` | Single track detail |
| `GET /api/failed` | Failed tracks |
| `GET /api/pending` | Pending tracks |
| `GET /api/logs` | Recent sync log entries |
| `GET /api/sync-state` | Sync engine state (cursors, timestamps) |
| `GET /docs` | Swagger UI |

## Observability

### Prometheus Metrics

All metrics are prefixed with `navaar_`. Key metrics:

- `navaar_sync_cycles_total{direction}` — Total sync cycles run
- `navaar_tracks_synced_total{direction}` — Tracks successfully synced
- `navaar_tracks_discovered_total{direction}` — Tracks discovered
- `navaar_sync_errors_total{direction,error_type}` — Sync errors
- `navaar_tracks_pending{direction}` — Current pending track gauge
- `navaar_tracks_failed{direction}` — Current failed track gauge
- `navaar_sync_cycle_duration_seconds{direction}` — Cycle duration histogram
- `navaar_up` — Service health (1 = up, 0 = down)

The pod has Prometheus scrape annotations for automatic discovery.

### Structured Logging

JSON-formatted logs via structlog (pretty-printed in TTY mode). Includes correlation via track IDs and sync cycle context.

## Deployment

The project includes Kubernetes manifests and an ArgoCD application for GitOps deployment.

```
deploy/
├── Dockerfile
├── docker-compose.yml
├── k8s/
│   ├── namespace.yaml
│   ├── configmap.yaml
│   ├── secret.yaml          # template — real values applied manually
│   ├── deployment.yaml       # Recreate strategy, node affinity
│   ├── service.yaml
│   └── pvc.yaml              # 256Mi local-path
└── argocd/
    └── application.yaml      # auto-sync with self-heal
```

CI/CD pipeline: push to `main` → GitHub Actions runs tests → builds Docker image → pushes to GHCR → ArgoCD syncs to cluster.

## Project Structure

```
src/navaar/
├── __main__.py          # Entry point, wires all components
├── config.py            # pydantic-settings configuration
├── metrics.py           # Prometheus metric definitions
├── api/
│   └── server.py        # FastAPI health, metrics, JSON API
├── db/
│   ├── models.py        # SQLAlchemy ORM models
│   ├── engine.py        # Async engine + session factory
│   └── repository.py    # Data access layer
├── telegram/
│   ├── bot.py           # Command handlers + channel post handler
│   └── client.py        # Download/upload wrappers
├── ytmusic/
│   ├── client.py        # YouTube Music API wrapper
│   └── downloader.py    # yt-dlp wrapper
└── sync/
    ├── engine.py         # Polling loop orchestrator
    ├── tg_to_yt.py       # Telegram → YouTube Music sync
    ├── yt_to_tg.py       # YouTube Music → Telegram sync
    └── identifier.py     # Track identification pipeline
```

## Testing

```bash
uv run pytest tests/ -v
```

## Tech Stack

- [python-telegram-bot](https://python-telegram-bot.org/) — Telegram bot framework (async)
- [ytmusicapi](https://github.com/sigma67/ytmusicapi) — YouTube Music API (unofficial, OAuth)
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) — YouTube audio download
- [SQLAlchemy](https://www.sqlalchemy.org/) + aiosqlite — Async SQLite persistence
- [FastAPI](https://fastapi.tiangolo.com/) — HTTP API
- [prometheus-client](https://github.com/prometheus/client_python) — Metrics
- [structlog](https://www.structlog.org/) — Structured logging
- [pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/) — Configuration
- [tenacity](https://tenacity.readthedocs.io/) — Retry logic
- [mutagen](https://mutagen.readthedocs.io/) — ID3 tag reading
