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

    Wraps lower-level driver errors in the
    :class:`DatabaseConnectionError` hierarchy so callers handle a single
    well-defined exception type. The engine establishes connections
    lazily; this call does not perform I/O. Caller owns disposal; call
    ``await engine.dispose()`` when the consuming service tears down.

    Args:
        config: Optional override. Defaults to
            :func:`parse_postgres_config_from_env`.

    Raises:
        DatabaseConnectionError: When DSN construction fails or a required
            ``POSTGRES_*`` env var is missing.

    Example:
        engine = create_db_engine()
        session_maker = create_session_maker(engine)
        async with session_maker() as session:
            ...
        await engine.dispose()
    """
    if config is None:
        config = parse_postgres_config_from_env()
    try:
        return create_async_engine(
            config.async_dsn,
            pool_size=config.pool_min_size,
            max_overflow=max(config.pool_max_size - config.pool_min_size, 0),
            pool_timeout=config.command_timeout,
        )
    except (SQLAlchemyError, OSError) as e:
        target = (
            f"postgresql://{config.user}@{config.host}:{config.port}/{config.database}"
        )
        raise DatabaseConnectionError(f"failed to open engine to {target}: {e}") from e


def create_session_maker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Create an :class:`async_sessionmaker` bound to ``engine``.

    ``expire_on_commit=False`` because async sessions in request-scoped
    services should not expire attributes after commit; the standard
    workaround for the SQLAlchemy async footgun where a committed
    instance becomes detached and re-access triggers a lazy load.

    Args:
        engine: The async engine to bind sessions to.

    Returns:
        Session factory used by callers as ``async with session_maker() as s:``.
    """
    return async_sessionmaker(engine, expire_on_commit=False)
