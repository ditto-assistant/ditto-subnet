"""Low-level asyncpg pool open.

This module is internal to ``ditto.db``. Callers should use
:func:`ditto.db.create_db_pool` (which wraps asyncpg errors in the
:class:`DatabaseConnectionError` hierarchy and pulls config from env by
default) rather than calling ``_create_pool`` directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import asyncpg

if TYPE_CHECKING:
    from ditto.db.config import PostgresConfig


async def _create_pool(config: PostgresConfig) -> asyncpg.Pool:
    """Open an asyncpg pool from a :class:`PostgresConfig`.

    The caller owns shutdown; close the pool with ``await pool.close()``
    when the consuming service tears down.
    """
    return await asyncpg.create_pool(
        dsn=config.dsn,
        min_size=config.pool_min_size,
        max_size=config.pool_max_size,
        command_timeout=config.command_timeout,
    )
