from __future__ import annotations

import asyncio
import signal
import sys
import time

import structlog
import uvicorn

from navaar.api.server import create_app
from navaar.config import Settings
from navaar.db.engine import close_db, get_session_factory, init_db
from navaar.db.repository import SyncLogRepository, SyncStateRepository, TrackRepository
from navaar.metrics import UP, init_metrics
from navaar.sync.engine import SyncEngine
from navaar.sync.tg_to_yt import TgToYtSync
from navaar.sync.yt_to_tg import YtToTgSync
from navaar.telegram.bot import NavaarBot
from navaar.telegram.client import TelegramClient
from navaar.ytmusic.client import YTMusicClient
from navaar.ytmusic.downloader import YTDownloader


def configure_logging(level: str) -> None:
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer() if sys.stderr.isatty() else structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(structlog, level.upper(), structlog.INFO) if hasattr(structlog, level.upper()) else 20
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


async def run() -> None:
    settings = Settings()
    configure_logging(settings.log_level)
    logger = structlog.get_logger()

    logger.info("navaar_starting", version="0.1.0")

    # Detect Spotify availability
    sp_enabled = bool(settings.spotify_client_id and settings.spotify_playlist_id)

    # Init metrics with all label combinations
    init_metrics(
        version="0.1.0",
        playlist_id=settings.ytmusic_playlist_id,
        sp_playlist_id=settings.spotify_playlist_id if sp_enabled else "",
    )

    # Init database
    await init_db(settings.database_url)
    sf = get_session_factory()
    logger.info("database_initialized")

    # Repositories
    track_repo = TrackRepository(sf)
    sync_state = SyncStateRepository(sf)
    sync_log = SyncLogRepository(sf)

    # YT Music client
    yt_client = YTMusicClient(
        auth_file=settings.ytmusic_auth_file,
        playlist_id=settings.ytmusic_playlist_id,
        client_id=settings.ytmusic_client_id,
        client_secret=settings.ytmusic_client_secret,
    )
    logger.info("ytmusic_initialized", playlist_id=settings.ytmusic_playlist_id)

    # Downloader
    downloader = YTDownloader(cookies_file=settings.ytdlp_cookies_file)

    # Spotify client (conditional)
    sp_client = None
    if sp_enabled:
        from navaar.spotify.client import SpotifyClient

        sp_client = SpotifyClient(
            client_id=settings.spotify_client_id,
            client_secret=settings.spotify_client_secret,
            redirect_uri=settings.spotify_redirect_uri,
            playlist_id=settings.spotify_playlist_id,
            cache_path=settings.spotify_cache_path,
        )
        logger.info("spotify_initialized", playlist_id=settings.spotify_playlist_id)

    # Telegram bot app
    bot_app_builder = NavaarBot(
        token=settings.telegram_bot_token,
        channel_id=settings.telegram_channel_id,
        admin_user_ids=settings.telegram_admin_user_ids,
        track_repo=track_repo,
        sync_log=sync_log,
        sync_state=sync_state,
        yt_client=yt_client,
        sp_client=sp_client,
    )
    tg_app = bot_app_builder.build_app()

    # Telegram client (for downloads/uploads)
    tg_client = TelegramClient(tg_app.bot, settings.telegram_channel_id)

    # Sync modules
    tg_to_yt = TgToYtSync(track_repo, sync_log, tg_client, yt_client)
    yt_to_tg = YtToTgSync(
        track_repo, sync_state, sync_log, tg_client, yt_client, downloader,
        sp_enabled=sp_enabled,
    )

    sync_modules: dict[str, object] = {
        "tg_to_yt": tg_to_yt,
        "yt_to_tg": yt_to_tg,
    }
    intervals: dict[str, int] = {
        "tg_to_yt": settings.sync_interval_tg_to_yt,
        "yt_to_tg": settings.sync_interval_yt_to_tg,
    }

    if sp_client:
        from navaar.sync.sp_to_tg import SpToTgSync
        from navaar.sync.sp_to_yt import SpToYtSync
        from navaar.sync.tg_to_sp import TgToSpSync
        from navaar.sync.yt_to_sp import YtToSpSync

        tg_to_sp = TgToSpSync(track_repo, sync_log, tg_client, sp_client)
        sp_to_tg = SpToTgSync(
            track_repo, sync_state, sync_log, tg_client, sp_client, yt_client, downloader
        )
        yt_to_sp = YtToSpSync(track_repo, sync_log, yt_client, sp_client)
        sp_to_yt = SpToYtSync(track_repo, sync_log, sp_client, yt_client)

        sync_modules.update({
            "tg_to_sp": tg_to_sp,
            "sp_to_tg": sp_to_tg,
            "yt_to_sp": yt_to_sp,
            "sp_to_yt": sp_to_yt,
        })
        intervals.update({
            "tg_to_sp": settings.sync_interval_tg_to_sp,
            "sp_to_tg": settings.sync_interval_sp_to_tg,
            "yt_to_sp": settings.sync_interval_yt_to_sp,
            "sp_to_yt": settings.sync_interval_sp_to_yt,
        })

    # Sync engine
    engine = SyncEngine(
        sync_modules=sync_modules,
        intervals=intervals,
        track_repo=track_repo,
        sync_state=sync_state,
    )
    bot_app_builder.set_sync_engine(engine)

    # FastAPI app
    start_time = time.time()
    api_app = create_app(
        track_repo=track_repo,
        sync_state=sync_state,
        sync_log=sync_log,
        start_time=start_time,
    )
    api_config = uvicorn.Config(
        api_app, host="0.0.0.0", port=settings.api_port, log_level="warning"
    )
    api_server = uvicorn.Server(api_config)

    UP.set(1)
    logger.info("navaar_ready", api_port=settings.api_port)

    # Shutdown handling
    shutdown_event = asyncio.Event()

    def handle_signal(sig: int, frame: object) -> None:
        logger.info("shutdown_signal_received", signal=sig)
        engine.request_shutdown()
        shutdown_event.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    # Start all components
    await tg_app.initialize()
    await tg_app.start()
    await tg_app.updater.start_polling(drop_pending_updates=True)

    tasks = [
        asyncio.create_task(engine.run(), name="sync_engine"),
        asyncio.create_task(api_server.serve(), name="api_server"),
    ]

    logger.info("all_components_started")

    # Wait for shutdown
    await shutdown_event.wait()

    logger.info("shutting_down")
    UP.set(0)

    # Stop components in order
    await tg_app.updater.stop()
    await tg_app.stop()
    await tg_app.shutdown()
    api_server.should_exit = True

    # Wait for tasks with timeout
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    await close_db()
    logger.info("navaar_stopped")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
