from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from navaar.db.models import Base

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


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


async def init_db(database_url: str) -> None:
    global _engine, _session_factory
    _engine = create_async_engine(database_url, echo=False)
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
