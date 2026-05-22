"""FastAPI ``Depends`` factories for shared per-request resources.

Resources owned by the lifespan (engine, session maker, chain client)
live on ``app.state``. These factories read from ``request.app.state``
rather than capturing references at import time so test fixtures can
swap real components via ``app.dependency_overrides`` without import-order
gymnastics.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import Request
    from sqlalchemy.ext.asyncio import AsyncSession

    from ditto.chain import ChainClient


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Yield a per-request :class:`AsyncSession` bound to the lifespan engine.

    Acquired from ``app.state.session_maker`` and released via the
    context manager once the response is sent. Handler code receives
    a session ready for ``select(...)`` / ``session.add(...)``.
    """
    session_maker = request.app.state.session_maker
    async with session_maker() as session:
        yield session


async def get_chain_client(request: Request) -> ChainClient:
    """Return the singleton :class:`ChainClient` opened during lifespan.

    The client is shared across requests; its underlying Pylon SDK manages
    its own internal HTTP connection pool, and concurrent calls are safe.
    """
    return request.app.state.chain
