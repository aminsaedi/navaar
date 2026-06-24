FROM python:3.12-slim AS base

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    nodejs \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev --no-install-project --frozen

COPY src/ src/
RUN uv sync --no-dev --frozen

EXPOSE 8080

VOLUME ["/data"]

ENV NAVAAR_DATABASE_URL=sqlite+aiosqlite:////data/navaar.db
ENV NAVAAR_YTMUSIC_AUTH_FILE=/data/oauth.json

# Put the venv's bin dir on PATH so console scripts installed into it (yt-dlp)
# resolve for subprocesses. `uv run` did this implicitly; invoking the venv python
# directly (CMD below) does not, which silently dropped yt-dlp off PATH and broke
# the audio-download sync directions. The downloader now also calls yt-dlp via
# `python -m yt_dlp`, so this is defence-in-depth.
ENV PATH="/app/.venv/bin:$PATH"

# Run the venv interpreter directly: works regardless of runtime uid (k8s runs
# this as a non-root user) and avoids `uv run` needing a writable cache at start.
CMD ["/app/.venv/bin/python", "-m", "navaar"]
