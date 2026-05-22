"""Test fixtures for the api_server unit tests.

Every test gets a fresh FastAPI app via :func:`create_api_server` so
``dependency_overrides`` stay isolated. The ASGI transport from httpx
does not run lifespan, which is what we want for unit tests: ``app.state``
attributes the handlers expect are populated by dependency overrides
or set on the app directly.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI

from ditto.api_server import ApiServerConfig, create_api_server
from ditto.api_server.dependencies import get_chain_client, get_session
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
    )
    if overrides:
        from dataclasses import replace

        return replace(base, **overrides)
    return base


@pytest.fixture
def app() -> Iterator[FastAPI]:
    """A fresh FastAPI app per test, with auto-cleared dependency overrides."""
    a = create_api_server(make_api_server_config())
    # Lifespan does not run under ASGITransport, so set the bits the
    # health endpoint reads via app.state directly.
    a.state.commit_hash = "test-commit"
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
        else:
            chain.get_latest_block = AsyncMock(return_value=MagicMock(number=42))
        return chain

    app.dependency_overrides[get_chain_client] = _fake_chain
