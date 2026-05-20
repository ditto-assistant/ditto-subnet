"""Factory functions for the Postgres data layer."""

from __future__ import annotations

from typing import TYPE_CHECKING

import asyncpg

from ditto.db.config import parse_postgres_config_from_env
from ditto.db.errors import DatabaseConnectionError
from ditto.db.pool import _create_pool

if TYPE_CHECKING:
    from ditto.db.config import PostgresConfig


async def create_db_pool(config: PostgresConfig | None = None) -> asyncpg.Pool:
    """Open the asyncpg pool with sensible defaults.

    Wraps asyncpg's lower-level connection errors in the
    :class:`DatabaseConnectionError` hierarchy so callers can catch a
    single, well-defined exception type. The caller owns shutdown; call
    ``await pool.close()`` when the consuming service tears down.

    Args:
        config: Optional override. Defaults to
            :func:`parse_postgres_config_from_env`.

    Raises:
        DatabaseConnectionError: When Postgres is unreachable, auth fails,
            or a required ``POSTGRES_*`` env var is missing.

    Example:
        pool = await create_db_pool()
        async with DatabaseConnection(pool) as conn:
            ...
    """
    if config is None:
        config = parse_postgres_config_from_env()
    try:
        return await _create_pool(config)
    except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError) as e:
        target = (
            f"postgresql://{config.user}@{config.host}:{config.port}/{config.database}"
        )
        raise DatabaseConnectionError(f"failed to open pool to {target}: {e}") from e
