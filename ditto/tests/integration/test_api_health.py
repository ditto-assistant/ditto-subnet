"""Integration test for ``GET /health`` against the live local stack.

Requires ``docker compose up postgres pylon`` (or ``make stack-up``) plus
``make migrate``. Exercises the real lifespan, real engine, real chain
client. Excluded from the default test run via the ``integration`` marker.

Run explicitly::

    uv run pytest -m integration ditto/tests/integration/
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI

from ditto.api_server import create_api_server, parse_api_server_config_from_env

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _running_app() -> AsyncIterator[FastAPI]:
    """Build the real app and drive its lifespan via the lifespan-context API."""
    config = parse_api_server_config_from_env(commit_hash="integration-test")
    app = create_api_server(config)
    # FastAPI exposes the lifespan via router.lifespan_context.
    async with app.router.lifespan_context(app):
        yield app


class TestApiHealthIntegration:
    """End-to-end happy path against the real stack."""

    async def test_health_returns_200_with_all_deps_up(self):
        async with (
            _running_app() as app,
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
            ) as client,
        ):
            response = await client.get("/health")
            body = response.json()

        assert response.status_code == 200, body
        assert body["status"] == "ok"
        assert body["db"] == "ok"
        assert body["chain"] == "ok"
        assert body["commit"] == "integration-test"
