from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NAVAAR_", env_file=".env")

    telegram_bot_token: str
    telegram_channel_id: int = -1003744100092
    telegram_admin_user_ids: list[int] = Field(default_factory=list)

    ytmusic_auth_file: str = "oauth.json"
    ytmusic_playlist_id: str = "PLuiEUR-229Ow9l3QVvnER7F1cHDmuFHRE"
    ytmusic_client_id: str = ""
    ytmusic_client_secret: str = ""
    ytdlp_cookies_file: str = ""

    sync_interval_tg_to_yt: int = 60
    sync_interval_yt_to_tg: int = 120
    max_retries: int = 3

    database_url: str = "sqlite+aiosqlite:///navaar.db"
    api_port: int = 8080
    log_level: str = "INFO"
