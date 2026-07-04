from __future__ import annotations

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from navaar.db.models import Base

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _apply_sqlite_pragmas(dbapi_conn: object, _record: object) -> None:
    """Set per-connection SQLite pragmas on every new connection.

    Six sync loops, the bot, the API server, and the in-pod agent all open
    independent sessions and write concurrently against a single SQLite file.
    Without these, a write that collides with another writer raises
    ``database is locked`` (SQLAlchemy ``OperationalError``) — which surfaces as a
    silently-failed sync cycle. WAL lets readers run concurrently with a single
    writer, and a generous ``busy_timeout`` makes a colliding writer wait for the
    lock instead of erroring out. ``synchronous=NORMAL`` is the safe, recommended
    durability level under WAL.

    ``journal_mode=WAL`` persists in the DB header (a no-op after the first run);
    ``busy_timeout`` and ``synchronous`` are per-connection, so they must be set on
    every connect. Guarded so a non-file DB (``:memory:``) stays a plain no-op-safe
    connection.
    """
    cur = dbapi_conn.cursor()
    try:
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.execute("PRAGMA synchronous=NORMAL")
    finally:
        cur.close()


async def _run_migrations(engine: AsyncEngine) -> None:
    """Run lightweight migrations for existing SQLite databases."""
    async with engine.begin() as conn:
        # Check if sp_track_id column exists
        result = await conn.execute(text("PRAGMA table_info(tracks)"))
        columns = {row[1] for row in result}
        if "sp_track_id" not in columns:
            await conn.execute(
                text("ALTER TABLE tracks ADD COLUMN sp_track_id VARCHAR(30)")
            )
        if "card_message_id" not in columns:
            await conn.execute(
                text("ALTER TABLE tracks ADD COLUMN card_message_id INTEGER")
            )
        if "added_by" not in columns:
            await conn.execute(
                text("ALTER TABLE tracks ADD COLUMN added_by VARCHAR(200)")
            )


async def init_db(database_url: str) -> None:
    global _engine, _session_factory
    # timeout: how long the DBAPI waits for a locked DB before erroring. Belt to
    # the busy_timeout pragma's braces (both cover the same lock-wait window).
    connect_args = {"timeout": 30} if database_url.startswith("sqlite") else {}
    _engine = create_async_engine(database_url, echo=False, connect_args=connect_args)
    if database_url.startswith("sqlite"):
        event.listen(_engine.sync_engine, "connect", _apply_sqlite_pragmas)
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await _run_migrations(_engine)


async def close_db() -> None:
    global _engine, _session_factory
    if _engine:
        await _engine.dispose()
    _engine = None
    _session_factory = None


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    if _session_factory is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    return _session_factory
