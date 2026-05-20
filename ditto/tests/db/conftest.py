"""Shared fixtures + builders for ditto.db tests."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ditto.db.config import PostgresConfig


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
def mock_conn() -> AsyncMock:
    """Build an AsyncMock standing in for an ``asyncpg.Connection``."""
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock(return_value="")
    return conn


@pytest.fixture
def mock_pool(mock_conn: AsyncMock) -> MagicMock:
    """Build a MagicMock standing in for an ``asyncpg.Pool``.

    ``acquire`` is an awaitable returning the same ``mock_conn`` so tests
    can assert against the connection without juggling distinct objects.
    """
    pool = MagicMock()
    pool.acquire = AsyncMock(return_value=mock_conn)
    pool.release = AsyncMock(return_value=None)
    pool.close = AsyncMock(return_value=None)
    return pool
