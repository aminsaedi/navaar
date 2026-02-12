FROM python:3.12-slim AS base

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
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

CMD ["uv", "run", "python", "-m", "navaar"]
