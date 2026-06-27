from __future__ import annotations

import asyncio
import logging
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
from navaar.telegram.cards import TrackCardService
from navaar.telegram.client import TelegramClient
from navaar.ytmusic.client import YTMusicClient
from navaar.ytmusic.downloader import YTDownloader


def configure_logging(level: str) -> None:
    # Shared processors. The exception-rendering step differs by output mode:
    # ConsoleRenderer (TTY) renders exc_info itself, but JSONRenderer does NOT —
    # without dict_tracebacks, `logger.error(..., exc_info=True)` would serialize
    # to a bare `"exc_info": true` with no traceback in production logs.
    processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.TimeStamper(fmt="iso"),
    ]
    if sys.stderr.isatty():
        processors.append(structlog.dev.ConsoleRenderer())
    else:
        # format_exc_info renders a plain traceback string into an "exception"
        # key. Preferred over dict_tracebacks here because the latter dumps frame
        # locals (which can contain auth tokens) into the logs.
        processors.append(structlog.processors.format_exc_info)
        processors.append(structlog.processors.JSONRenderer())

    log_level = logging.getLevelNamesMapping().get(level.upper(), logging.INFO)
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


async def run() -> None:
    settings = Settings()
    configure_logging(settings.log_level)
    logger = structlog.get_logger()

    logger.info("navaar_starting", version="0.1.0")

    # Detect Spotify availability (PKCE mode needs only playlist_id + cache)
    sp_enabled = bool(settings.spotify_playlist_id)

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
    downloader = YTDownloader(
        cookies_file=settings.ytdlp_cookies_file,
        max_upload_mb=settings.telegram_max_upload_mb,
    )

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

    # Track status cards: reply to each track in the channel and live-edit the
    # reply as the logical track syncs across platforms. Injected into the bot
    # (initial post on channel message) and every sync module (refresh on each
    # terminal state) so all six directions update the same card.
    card_service = TrackCardService(
        tg_app.bot,
        settings.telegram_channel_id,
        track_repo,
        sp_enabled=sp_enabled,
        enabled=settings.track_cards_enabled,
    )
    bot_app_builder.set_card_service(card_service)
    for module in sync_modules.values():
        module.set_card_service(card_service)

    # Alert notifier: push systemic sync failures to Telegram. Falls back to the
    # first admin DM when no dedicated alert chat is configured.
    from navaar.telegram.alerts import AlertNotifier

    alert_chat_id = settings.alert_chat_id or (
        settings.telegram_admin_user_ids[0] if settings.telegram_admin_user_ids else None
    )
    alert_notifier = AlertNotifier(
        bot=tg_app.bot,
        chat_id=alert_chat_id,
        enabled=settings.alert_enabled,
        consecutive_threshold=settings.alert_consecutive_crashes,
        cooldown_seconds=settings.alert_cooldown_seconds,
    )
    if not alert_chat_id:
        logger.warning("alert_notifier_disabled", reason="no_chat_id")

    # Sync engine
    engine = SyncEngine(
        sync_modules=sync_modules,
        intervals=intervals,
        track_repo=track_repo,
        sync_state=sync_state,
        alert_notifier=alert_notifier,
        backoff_max_seconds=settings.backoff_max_seconds,
        circuit_open_after=settings.circuit_open_after,
    )
    bot_app_builder.set_sync_engine(engine)

    # Natural-language control: the Claude Agent SDK runs Claude Code inside the pod
    # (Bash/file tools + a navaar MCP server) over the Anthropic endpoint. Live only
    # when enabled; otherwise the bot's NL handlers no-op.
    if settings.nl_agent_enabled:
        from navaar.telegram.agent import NavaarAgent

        agent = NavaarAgent(
            model=settings.nl_model,
            timeout=settings.nl_request_timeout,
            max_turns=settings.nl_max_turns,
            workspace_dir=settings.nl_workspace_dir,
            bot=tg_app.bot,
            channel_id=settings.telegram_channel_id,
            track_repo=track_repo,
            engine=engine,
            card_service=card_service,
            yt_client=yt_client,
            sp_client=sp_client,
            sp_enabled=sp_enabled,
        )
        bot_app_builder.set_agent(agent)
        logger.info("nl_agent_enabled", model=settings.nl_model)

    # FastAPI app
    start_time = time.time()
    api_app = create_app(
        track_repo=track_repo,
        sync_state=sync_state,
        sync_log=sync_log,
        start_time=start_time,
        intervals=intervals,
        stale_multiplier=settings.readiness_stale_multiplier,
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
    # Populate the Telegram `/` command menu (setMyCommands) for admins.
    await bot_app_builder.set_command_menu()

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
