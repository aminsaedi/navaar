from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NAVAAR_", env_file=".env")

    telegram_bot_token: str
    telegram_channel_id: int = -1003744100092
    telegram_admin_user_ids: list[int] = Field(default_factory=list)
    # Max audio upload size (MiB). Files larger than this are re-encoded to a
    # lower bitrate that fits. 50 is the standard Bot API limit; raise it only
    # behind a self-hosted Telegram Bot API server.
    telegram_max_upload_mb: int = 50

    ytmusic_auth_file: str = "oauth.json"
    ytmusic_playlist_id: str = "PLuiEUR-229Ow9l3QVvnER7F1cHDmuFHRE"
    ytmusic_client_id: str = ""
    ytmusic_client_secret: str = ""
    ytdlp_cookies_file: str = ""

    spotify_client_id: str = ""
    spotify_client_secret: str = ""
    spotify_redirect_uri: str = ""
    spotify_cache_path: str = ".spotify_cache"
    spotify_playlist_id: str = ""

    sync_interval_tg_to_yt: int = 60
    sync_interval_yt_to_tg: int = 120
    sync_interval_tg_to_sp: int = 60
    sync_interval_sp_to_tg: int = 120
    sync_interval_yt_to_sp: int = 120
    sync_interval_sp_to_yt: int = 120
    max_retries: int = 3

    database_url: str = "sqlite+aiosqlite:///navaar.db"
    api_port: int = 8080
    log_level: str = "INFO"

    # Alerting: push systemic sync failures to Telegram. Falls back to the first
    # admin user id when alert_chat_id is 0. Set 0 + no admins to disable.
    alert_enabled: bool = True
    alert_chat_id: int = 0
    alert_consecutive_crashes: int = 2  # systemic threshold (auth errors alert on first)
    alert_cooldown_seconds: int = 1800  # re-alert window for an still-open incident

    # Per-direction backoff after repeated cycle crashes (caps the retry storm).
    backoff_max_seconds: int = 1800
    circuit_open_after: int = 5  # consecutive crashes before a direction reports unhealthy

    # /readyz reports degraded if a direction hasn't completed a cycle within
    # this multiple of its interval (catches silent crash-loops).
    readiness_stale_multiplier: int = 5
