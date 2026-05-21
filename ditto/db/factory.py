"""Factory functions for the Postgres data layer."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ditto.db.config import parse_postgres_config_from_env
from ditto.db.errors import DatabaseConnectionError

if TYPE_CHECKING:
    from ditto.db.config import PostgresConfig


def create_db_engine(config: PostgresConfig | None = None) -> AsyncEngine:
    """Create an async SQLAlchemy engine over the asyncpg driver.

    Wraps lower-level driver errors in :class:`DatabaseConnectionError`.
    Connections are opened lazily; this call does not perform I/O.
    Caller owns disposal via ``await engine.dispose()``.

    Raises:
        DatabaseConnectionError: When DSN construction fails or a required
            ``POSTGRES_*`` env var is missing.
    """
    if config is None:
        config = parse_postgres_config_from_env()
    try:
        return create_async_engine(
            config.async_dsn,
            pool_size=config.pool_min_size,
            max_overflow=max(config.pool_max_size - config.pool_min_size, 0),
            pool_pre_ping=True,
            pool_recycle=3600,
            # command_timeout is asyncpg's per-query timeout. SA's pool_timeout
            # is an unrelated pool-acquisition wait, so route via connect_args.
            connect_args={"command_timeout": config.command_timeout},
        )
    except (SQLAlchemyError, OSError) as e:
        target = (
            f"postgresql://{config.user}@{config.host}:{config.port}/{config.database}"
        )
        raise DatabaseConnectionError(f"failed to open engine to {target}: {e}") from e


def create_session_maker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Create an :class:`async_sessionmaker` bound to ``engine``.

    ``expire_on_commit=False`` so attribute access on committed instances
    does not trigger an unwanted lazy load (standard async-SA pattern).
    """
    return async_sessionmaker(engine, expire_on_commit=False)
