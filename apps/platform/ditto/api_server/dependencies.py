"""FastAPI ``Depends`` factories for per-request resources."""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_server.benchmark_rollout import inference_activation_requirements
from ditto.api_server.datapipeline import DatasetGenerator
from ditto.api_server.embedding import Embedder
from ditto.api_server.payment_verifier import PaymentVerifier
from ditto.api_server.pricing import PriceOracle
from ditto.api_server.storage import S3StorageClient
from ditto.chain import ChainClient
from ditto.db.queries.benchmark_rollout import (
    CANARY_BENCH_VERSION,
    bind_inference_activation_requirements,
)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Yield an :class:`AsyncSession` from the lifespan's session maker."""
    session_maker = request.app.state.session_maker
    async with session_maker() as session:
        bind_inference_activation_requirements(
            session,
            inference_activation_requirements(
                request.app.state.config.inference_proxy,
                bench_version=CANARY_BENCH_VERSION,
            ),
        )
        yield session


async def get_chain_client(request: Request) -> ChainClient:
    """Return the lifespan-opened :class:`ChainClient`."""
    return request.app.state.chain


async def get_price_oracle(request: Request) -> PriceOracle:
    """Return the lifespan-opened :class:`PriceOracle`."""
    return request.app.state.price_oracle


async def get_payment_verifier(request: Request) -> PaymentVerifier:
    """Return the lifespan-instantiated :class:`PaymentVerifier`."""
    return request.app.state.payment_verifier


async def get_storage_client(request: Request) -> S3StorageClient:
    """Return the lifespan-opened :class:`S3StorageClient`."""
    return request.app.state.storage


async def get_embedder(request: Request) -> Embedder:
    """Return the lifespan-created :class:`Embedder` (null when disabled)."""
    return request.app.state.embedder


async def get_dataset_generator(request: Request) -> DatasetGenerator:
    """Return the lifespan-created :class:`DatasetGenerator` (null when disabled)."""
    return request.app.state.dataset_generator
