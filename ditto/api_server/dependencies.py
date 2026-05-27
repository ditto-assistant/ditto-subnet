"""FastAPI ``Depends`` factories for per-request resources."""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_server.pricing import PriceOracle
from ditto.chain import ChainClient


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Yield an :class:`AsyncSession` from the lifespan's session maker."""
    session_maker = request.app.state.session_maker
    async with session_maker() as session:
        yield session


async def get_chain_client(request: Request) -> ChainClient:
    """Return the lifespan-opened :class:`ChainClient`."""
    return request.app.state.chain


async def get_price_oracle(request: Request) -> PriceOracle:
    """Return the lifespan-opened :class:`PriceOracle`."""
    return request.app.state.price_oracle
