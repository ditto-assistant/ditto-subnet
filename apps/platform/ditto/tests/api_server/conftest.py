"""Test fixtures for the api_server unit tests.

Every test gets a fresh FastAPI app via :func:`create_api_server` so
``dependency_overrides`` stay isolated. The ASGI transport from httpx
does not run lifespan, which is what we want for unit tests: ``app.state``
attributes the handlers expect are populated by dependency overrides
or set on the app directly.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI

from ditto.api_server import (
    ApiServerConfig,
    ScreenerAuthConfig,
    ValidatorCompatibilityConfig,
    ValidatorNamesConfig,
    create_api_server,
)
from ditto.api_server.datapipeline import DataPipelineConfig, NullGenerator
from ditto.api_server.dependencies import (
    get_chain_client,
    get_embedder,
    get_price_oracle,
    get_session,
    get_storage_client,
)
from ditto.api_server.embedding import EmbeddingConfig, NullEmbedder
from ditto.api_server.pricing import PricingConfig
from ditto.api_server.storage import ObjectMetadata, StorageConfig
from ditto.chain import ChainConfig
from ditto.db.config import PostgresConfig


def make_api_server_config(**overrides: Any) -> ApiServerConfig:
    """Build an :class:`ApiServerConfig` for tests.

    Lifespan-opened deps are never exercised in unit tests, so the
    postgres / chain sub-configs only need to be structurally valid.
    """
    base = ApiServerConfig(
        host="127.0.0.1",
        port=8000,
        log_level="INFO",
        commit_hash="test-commit",
        upload_payment_address="5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
        postgres=PostgresConfig(
            host="localhost",
            port=5432,
            user="ditto",
            password="ditto",
            database="ditto",
        ),
        chain=ChainConfig(
            pylon_url="http://pylon:8001",
            netuid=118,
            open_access_token="test-token",
        ),
        pricing=PricingConfig(
            fee_usd=Decimal("5"),
            fee_buffer=Decimal("1.4"),
            cache_ttl_seconds=3600,
            max_stale_seconds=86400,
            coingecko_timeout_seconds=5.0,
            override_tao_usd=None,
        ),
        storage=StorageConfig(
            endpoint_url="http://minio:9000",
            bucket="ditto-agents",
            access_key="minio",
            secret_key="miniominio",
        ),
        embedding=EmbeddingConfig(
            url=None,  # code-embedding disabled in unit tests (null embedder)
            model="",
            revision="main",
            dim=None,
            timeout_seconds=5.0,
            auth="none",
        ),
        data_pipeline=DataPipelineConfig(
            url=None,  # generate service disabled in unit tests (null generator)
            run_size="full",
            timeout_seconds=30.0,
            auth="none",
        ),
        screener_auth=ScreenerAuthConfig(
            hotkey="5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
            api_token="test-screener-token-at-least-32-characters",
        ),
        validator_names=ValidatorNamesConfig(),
        validator_compatibility=ValidatorCompatibilityConfig(
            minimum_software_version=None,
            minimum_protocol_version=1,
            heartbeat_max_age_seconds=300,
        ),
    )
    if overrides:
        from dataclasses import replace

        return replace(base, **overrides)
    return base


@pytest.fixture
def app() -> Iterator[FastAPI]:
    """A fresh FastAPI app per test, with auto-cleared dependency overrides."""
    # Endpoint tests assert per-request behavior; the public TTL cache would
    # serve stale bodies across a test's mutate-then-refetch sequence. The
    # middleware has its own coverage in tests/api_server/middleware.
    os.environ["PUBLIC_CACHE_DISABLED"] = "1"
    a = create_api_server(make_api_server_config())
    # Lifespan does not run under ASGITransport, so set the bits the
    # health endpoint reads via app.state directly.
    a.state.commit_hash = "test-commit"
    # code embedder is lifespan-created; default it to the disabled null embedder so
    # upload tests get a null vector unless they override get_embedder.
    a.state.embedder = NullEmbedder()
    # dataset generator is lifespan-created; default to the disabled null generator
    # so verdict tests promote without pinning a dataset unless they override it.
    a.state.dataset_generator = NullGenerator()
    # storage is lifespan-opened; default to a mock with the public mirror OFF
    # (public_bucket=None) so score-submit tests run without S3 unless they
    # override get_storage_client.
    from unittest.mock import MagicMock

    default_storage = MagicMock()
    default_storage.public_bucket = None

    async def _head_screened_image(*, key: str) -> ObjectMetadata:
        agent_id = key.split("/", 1)[0]
        return ObjectMetadata(
            size_bytes=123,
            metadata={
                "sha256": "12" * 32,
                "image-id": "sha256:" + "34" * 32,
                "image-ref": f"ditto-screen/{agent_id}:latest",
            },
        )

    default_storage.head_object = AsyncMock(side_effect=_head_screened_image)
    a.state.storage = default_storage
    yield a
    a.dependency_overrides.clear()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """``httpx.AsyncClient`` wired to the per-test FastAPI app.

    ``raise_app_exceptions=False`` lets the unhandled-exception envelope
    handler return its JSON response instead of httpx re-raising the
    original error to the test (which would defeat the purpose of the
    handler).
    """
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as c:
        yield c


def override_get_session(app: FastAPI, *, raises: Exception | None = None) -> None:
    """Install a ``get_session`` override that either yields a mock or raises."""

    async def _fake_session() -> AsyncIterator[MagicMock]:
        session = MagicMock()
        if raises is not None:
            session.execute = AsyncMock(side_effect=raises)
        else:
            session.execute = AsyncMock(return_value=None)
        yield session

    app.dependency_overrides[get_session] = _fake_session


def override_get_chain_client(app: FastAPI, *, raises: Exception | None = None) -> None:
    """Install a ``get_chain_client`` override that returns a mock client."""

    async def _fake_chain() -> MagicMock:
        chain = MagicMock()
        if raises is not None:
            chain.get_latest_block = AsyncMock(side_effect=raises)
            chain.is_registered = AsyncMock(side_effect=raises)
            chain.get_registered_coldkey = AsyncMock(side_effect=raises)
            chain.get_weights = AsyncMock(side_effect=raises)
        else:
            chain.get_latest_block = AsyncMock(return_value=MagicMock(number=42))
            chain.is_registered = AsyncMock(return_value=True)
            chain.get_registered_coldkey = AsyncMock(return_value="5Coldkey")
            # Empty revealed weight matrix by default: the king-weight sweep on
            # the score path finds nothing to confirm unless a test says otherwise.
            chain.get_weights = AsyncMock(return_value=MagicMock(vectors=()))
        return chain

    app.dependency_overrides[get_chain_client] = _fake_chain


def override_get_price_oracle(
    app: FastAPI,
    *,
    price_usd: Decimal | None = None,
    raises: Exception | None = None,
) -> None:
    """Install a ``get_price_oracle`` override that returns a mock oracle."""

    async def _fake_oracle() -> MagicMock:
        oracle = MagicMock()
        if raises is not None:
            oracle.get_tao_usd = AsyncMock(side_effect=raises)
        else:
            oracle.get_tao_usd = AsyncMock(
                return_value=price_usd if price_usd is not None else Decimal("400")
            )
        return oracle

    app.dependency_overrides[get_price_oracle] = _fake_oracle


def override_get_storage_client(
    app: FastAPI,
    *,
    raises: Exception | None = None,
) -> MagicMock:
    """Install a ``get_storage_client`` override returning a mock client.

    Returns the mock so tests can inspect ``put_object`` call args.
    """
    storage = MagicMock()
    if raises is not None:
        storage.put_object = AsyncMock(side_effect=raises)
        storage.object_exists = AsyncMock(side_effect=raises)
    else:
        storage.put_object = AsyncMock(return_value=MagicMock())
        storage.object_exists = AsyncMock(return_value=True)

    async def _fake_storage() -> MagicMock:
        return storage

    app.dependency_overrides[get_storage_client] = _fake_storage
    return storage


def override_get_embedder(app: FastAPI, *, vector: list[float] | None = None) -> None:
    """Install a ``get_embedder`` override returning a stub embedder.

    Defaults to a null embedder (``None`` vector). Pass ``vector`` to simulate an
    enabled service returning that fixed embedding with a ``stub@test`` model tag.
    """

    class _StubEmbedder:
        model_tag = "stub@test" if vector is not None else None

        async def embed(self, text: str) -> list[float] | None:
            del text
            return vector

        async def aclose(self) -> None:
            return None

    async def _fake_embedder() -> _StubEmbedder:
        return _StubEmbedder()

    app.dependency_overrides[get_embedder] = _fake_embedder
