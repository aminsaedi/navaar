FROM python:3.12-slim AS base

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    nodejs \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Claude Code CLI — the engine the Claude Agent SDK (claude-agent-sdk) drives for
# the bot's natural-language agent. The native installer ships a self-contained
# binary (auto-detects arch, no Node needed); copy it onto PATH for the non-root
# runtime uid. (Debian's `npm` is avoided — on trixie it pulls a 400-package tree.)
RUN curl -fsSL https://claude.ai/install.sh | bash -s -- 2.1.195 \
    && cp "$(readlink -f "$HOME/.local/bin/claude")" /usr/local/bin/claude \
    && chmod 755 /usr/local/bin/claude \
    && claude --version

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

# Keep the Claude Code CLI hermetic and non-interactive in the pod (no auto-update,
# telemetry, or error reporting — it runs as a non-root uid against a private endpoint).
ENV DISABLE_AUTOUPDATER=1 \
    DISABLE_TELEMETRY=1 \
    DISABLE_ERROR_REPORTING=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1

# Run the venv interpreter directly: works regardless of runtime uid (k8s runs
# this as a non-root user) and avoids `uv run` needing a writable cache at start.
CMD ["/app/.venv/bin/python", "-m", "navaar"]
