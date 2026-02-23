# Navaar (نوار)

Three-way music sync between a Telegram channel, a YouTube Music playlist, and a Spotify playlist.

When audio is posted to any of the three services, Navaar automatically syncs it to the other two. Six sync directions run concurrently as configurable polling loops with duplicate detection, retry logic, and full audit logging.

## How It Works

**Telegram → YouTube Music + Spotify**
1. Audio file posted to the Telegram channel
2. Track identified via ID3 tags, Telegram metadata, or filename
3. YouTube Music and Spotify searched for matches
4. Matches added to both playlists

**YouTube Music → Telegram + Spotify**
1. YT Music playlist polled for new tracks
2. New tracks downloaded via yt-dlp and uploaded to Telegram
3. Spotify searched for the same track and added to the Spotify playlist

**Spotify → Telegram + YouTube Music**
1. Spotify playlist polled for new tracks
2. YouTube searched for the same track (Spotify has no download API)
3. Audio downloaded via yt-dlp and uploaded to Telegram
4. Track also added to the YT Music playlist

All six directions run as concurrent polling loops (defaults: 60s for TG→YT/TG→SP, 120s for all others). Cross-service deduplication prevents sync loops.

## Setup

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) package manager
- ffmpeg (for audio processing)
- A Telegram bot with admin access to the target channel
- Google OAuth credentials for YouTube Music API
- A Spotify account (free tier works with PKCE auth)

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

### Spotify OAuth (PKCE)

Run the bootstrap script to complete the one-time browser OAuth flow:

```bash
uv run python scripts/spotify_auth.py
```

This uses PKCE auth with a public client ID — no Spotify Premium or developer app required. The token is saved to `.spotify_cache`. For deployment, copy this file to the data volume.

To use your own Spotify app credentials instead:

```bash
NAVAAR_SPOTIFY_CLIENT_ID=xxx NAVAAR_SPOTIFY_CLIENT_SECRET=yyy uv run python scripts/spotify_auth.py
```

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
| `NAVAAR_YTDLP_COOKIES_FILE` | No | `""` | Path to cookies.txt for yt-dlp |
| `NAVAAR_SPOTIFY_CLIENT_ID` | No | `""` | Spotify client ID (uses public PKCE client if empty) |
| `NAVAAR_SPOTIFY_CLIENT_SECRET` | No | `""` | Spotify client secret (PKCE mode if empty) |
| `NAVAAR_SPOTIFY_REDIRECT_URI` | No | `""` | Spotify OAuth redirect URI |
| `NAVAAR_SPOTIFY_CACHE_PATH` | No | `.spotify_cache` | Path to Spotify token cache |
| `NAVAAR_SPOTIFY_PLAYLIST_ID` | No | `""` | Spotify playlist ID (enables Spotify sync) |
| `NAVAAR_SYNC_INTERVAL_TG_TO_YT` | No | `60` | Seconds between TG→YT sync cycles |
| `NAVAAR_SYNC_INTERVAL_YT_TO_TG` | No | `120` | Seconds between YT→TG sync cycles |
| `NAVAAR_SYNC_INTERVAL_TG_TO_SP` | No | `60` | Seconds between TG→SP sync cycles |
| `NAVAAR_SYNC_INTERVAL_SP_TO_TG` | No | `120` | Seconds between SP→TG sync cycles |
| `NAVAAR_SYNC_INTERVAL_YT_TO_SP` | No | `120` | Seconds between YT→SP sync cycles |
| `NAVAAR_SYNC_INTERVAL_SP_TO_YT` | No | `120` | Seconds between SP→YT sync cycles |
| `NAVAAR_MAX_RETRIES` | No | `3` | Max retry attempts for failed tracks |
| `NAVAAR_DATABASE_URL` | No | `sqlite+aiosqlite:///navaar.db` | SQLAlchemy database URL |
| `NAVAAR_API_PORT` | No | `8080` | HTTP API port |
| `NAVAAR_LOG_LEVEL` | No | `INFO` | Log level (DEBUG, INFO, WARNING, ERROR) |

Spotify sync is enabled when `NAVAAR_SPOTIFY_PLAYLIST_ID` is set. Without it, only the TG↔YT directions run.

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

Data (SQLite database, OAuth tokens, Spotify cache) persists in the `navaar-data` volume.

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
| `/sync [tg\|yt\|sp\|all]` | Force immediate sync cycle |
| `/retry <id\|all\|tg\|yt\|sp>` | Retry failed tracks |
| `/delete <id>` | Remove a track from the database |
| `/search <query>` | Search YouTube Music |
| `/search_sp <query>` | Search Spotify |
| `/failed [tg\|yt\|sp]` | List failed tracks with reasons |
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
| `GET /api/pending` | Pending tracks by direction |
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
- `navaar_sp_search_total{result}` — Spotify search results (found/not_found)
- `navaar_sp_search_duration_seconds` — Spotify search latency
- `navaar_up` — Service health (1 = up, 0 = down)

Directions: `tg_to_yt`, `yt_to_tg`, `tg_to_sp`, `sp_to_tg`, `yt_to_sp`, `sp_to_yt`.

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

Secrets are managed manually in-cluster (ArgoCD ignores Secret data diffs via `ignoreDifferences`). The Spotify `.spotify_cache` file must be copied to the PVC's `/data` directory.

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
│   ├── engine.py        # Async engine + session factory + migrations
│   └── repository.py    # Data access layer
├── telegram/
│   ├── bot.py           # Command handlers + channel post handler
│   └── client.py        # Download/upload wrappers
├── ytmusic/
│   ├── client.py        # YouTube Music API wrapper (OAuth)
│   └── downloader.py    # yt-dlp wrapper
├── spotify/
│   └── client.py        # Spotify API wrapper (PKCE/OAuth)
└── sync/
    ├── engine.py         # N-direction polling loop orchestrator
    ├── tg_to_yt.py       # Telegram → YouTube Music
    ├── yt_to_tg.py       # YouTube Music → Telegram (+ fan-out)
    ├── tg_to_sp.py       # Telegram → Spotify
    ├── sp_to_tg.py       # Spotify → Telegram (+ fan-out)
    ├── yt_to_sp.py       # YouTube Music → Spotify
    ├── sp_to_yt.py       # Spotify → YouTube Music
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
- [spotipy](https://spotipy.readthedocs.io/) — Spotify Web API (PKCE/OAuth)
- [SQLAlchemy](https://www.sqlalchemy.org/) + aiosqlite — Async SQLite persistence
- [FastAPI](https://fastapi.tiangolo.com/) — HTTP API
- [prometheus-client](https://github.com/prometheus/client_python) — Metrics
- [structlog](https://www.structlog.org/) — Structured logging
- [pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/) — Configuration
- [tenacity](https://tenacity.readthedocs.io/) — Retry logic
- [mutagen](https://mutagen.readthedocs.io/) — ID3 tag reading
