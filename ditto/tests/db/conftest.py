"""Shared fixtures + builders for ditto.db tests.

Unit tests use SQLite-in-memory via ``aiosqlite`` so the SQLAlchemy ORM
is exercised against a real database engine rather than against mocks
of the session API. SQLite handles every feature our schema needs
(composite PK + FK, UNIQUE, CHECK, partial indexes); Postgres-specific
quirks are covered by integration tests in a later layer.

``PRAGMA foreign_keys=ON`` is enabled per connection because SQLite
does not enforce foreign keys by default and the data layer relies on
the composite FK on ``evaluation_payments``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ditto.db.config import PostgresConfig
from ditto.db.models import Base


def make_postgres_config(**overrides: Any) -> PostgresConfig:
    """Build a :class:`PostgresConfig` with sensible defaults.

    Defaults match the docker-compose service; tests override only the
    fields they care about.
    """
    base: dict[str, Any] = {
        "host": "localhost",
        "port": 5432,
        "user": "ditto",
        "password": "ditto",
        "database": "ditto",
        "pool_min_size": 2,
        "pool_max_size": 10,
        "command_timeout": 30.0,
    }
    base.update(overrides)
    return PostgresConfig(**base)


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    """Per-test SQLite-in-memory async engine with the full schema applied."""
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")

    # SQLite ignores FK constraints unless PRAGMA foreign_keys=ON is set on
    # every connection. Enable it via an event hook so the composite FK on
    # evaluation_payments behaves the way the production schema requires.
    @event.listens_for(eng.sync_engine, "connect")
    def _enable_fk(dbapi_connection: Any, _: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest.fixture
async def session_maker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Session factory bound to the in-memory engine."""
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
async def session(
    session_maker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """One :class:`AsyncSession` scoped to a single test function."""
    async with session_maker() as sess:
        yield sess
