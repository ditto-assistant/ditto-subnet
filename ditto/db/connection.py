"""Async connection acquisition for the Postgres data layer.

:class:`DatabaseConnection` is the canonical way to obtain a pooled
connection. Nested ``async with DatabaseConnection(pool)`` scopes reuse
the same underlying connection via a contextvar, so query helpers can
delegate to nested helpers and share one connection (and one
transaction, when wrapped explicitly) without the caller threading the
connection through every signature.

The :func:`db_operation` decorator opens such a scope for the lifetime
of a decorated query function.
"""

from __future__ import annotations

import contextvars
import functools
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    import asyncpg

logger = logging.getLogger(__name__)

T = TypeVar("T")


_connection_var: contextvars.ContextVar[asyncpg.Connection | None] = (
    contextvars.ContextVar(
        "ditto_db_connection",
        default=None,
    )
)


class DatabaseConnection:
    """Async context manager that acquires a pooled connection.

    First use in a context acquires from the pool and stores the
    connection in a contextvar. Nested ``async with`` blocks observe
    the existing value and reuse it without acquiring a new connection;
    the inner ``__aexit__`` is a no-op and only the outer scope releases.

    Usage:
        async with DatabaseConnection(pool) as conn:
            row = await conn.fetchrow(
                "SELECT * FROM agents WHERE agent_id = $1",
                agent_id,
            )
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool
        self._conn: asyncpg.Connection | None = None
        self._token: contextvars.Token[asyncpg.Connection | None] | None = None
        self._reused = False

    async def __aenter__(self) -> asyncpg.Connection:
        existing = _connection_var.get()
        if existing is not None:
            self._conn = existing
            self._reused = True
            return existing
        self._conn = await self._pool.acquire()
        self._token = _connection_var.set(self._conn)
        return self._conn

    async def __aexit__(
        self,
        exc_type: Any,
        exc: Any,
        tb: Any,
    ) -> None:
        if self._reused:
            return
        try:
            if self._conn is not None:
                await self._pool.release(self._conn)
        finally:
            if self._token is not None:
                _connection_var.reset(self._token)


def db_operation(
    fn: Callable[..., Awaitable[T]],
) -> Callable[..., Awaitable[T]]:
    """Decorate a query function so a connection is always acquired.

    The wrapped function's first argument must be ``pool: asyncpg.Pool``.
    The decorator opens a :class:`DatabaseConnection` scope for the
    duration of the call so the body can use the connection via
    ``_connection_var.get()`` or by delegating to other ``@db_operation``
    functions that share the same pooled connection.
    """

    @functools.wraps(fn)
    async def wrapper(pool: asyncpg.Pool, *args: Any, **kwargs: Any) -> T:
        async with DatabaseConnection(pool):
            return await fn(pool, *args, **kwargs)

    return wrapper
