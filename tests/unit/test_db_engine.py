from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from navaar.db import engine as engine_mod
from navaar.db.engine import close_db, get_session_factory, init_db


@pytest.fixture
async def file_db(tmp_path: Path):
    """A real file-backed SQLite DB (WAL/busy_timeout are meaningful only off :memory:)."""
    url = f"sqlite+aiosqlite:///{tmp_path / 'navaar.db'}"
    await init_db(url)
    yield
    await close_db()


async def test_pragmas_applied_to_file_db(file_db) -> None:
    sf = get_session_factory()
    async with sf() as session:
        journal = (await session.execute(text("PRAGMA journal_mode"))).scalar()
        busy = (await session.execute(text("PRAGMA busy_timeout"))).scalar()
        sync = (await session.execute(text("PRAGMA synchronous"))).scalar()
    assert str(journal).lower() == "wal"
    assert int(busy) == 30000
    # synchronous=NORMAL -> 1
    assert int(sync) == 1


async def test_pragmas_applied_on_every_connection(file_db) -> None:
    # busy_timeout is per-connection; verify a second, independently-acquired
    # connection also carries it (regression guard for the connect listener).
    sf = get_session_factory()
    async with sf() as s1:
        assert int((await s1.execute(text("PRAGMA busy_timeout"))).scalar()) == 30000
    async with sf() as s2:
        assert int((await s2.execute(text("PRAGMA busy_timeout"))).scalar()) == 30000


async def test_init_db_memory_still_works() -> None:
    # The guard must not break a non-file DB; :memory: has no WAL but must init.
    await init_db("sqlite+aiosqlite:///:memory:")
    try:
        sf = get_session_factory()
        async with sf() as session:
            assert (await session.execute(text("SELECT 1"))).scalar() == 1
    finally:
        await close_db()
        # Reset module globals so later tests importing engine_mod see a clean slate.
        assert engine_mod._engine is None
