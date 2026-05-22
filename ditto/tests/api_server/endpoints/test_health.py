"""Unit tests for :mod:`ditto.api_server.endpoints.health`."""

from __future__ import annotations

import httpx
from fastapi import FastAPI
from sqlalchemy.exc import OperationalError

from ditto.chain.errors import ChainConnectionError
from ditto.tests.api_server.conftest import (
    override_get_chain_client,
    override_get_session,
)


class TestHealthHappyPath:
    """Both deps reachable - HTTP 200 with everything ``"ok"``."""

    async def test_returns_200(self, app: FastAPI, client: httpx.AsyncClient):
        override_get_session(app)
        override_get_chain_client(app)

        response = await client.get("/health")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["db"] == "ok"
        assert body["chain"] == "ok"

    async def test_returns_commit_hash_from_app_state(
        self, app: FastAPI, client: httpx.AsyncClient
    ):
        override_get_session(app)
        override_get_chain_client(app)
        app.state.commit_hash = "deadbeef"

        response = await client.get("/health")

        assert response.json()["commit"] == "deadbeef"


class TestHealthDbDown:
    """DB unreachable - HTTP 503 with ``db: down``."""

    async def test_returns_503(self, app: FastAPI, client: httpx.AsyncClient):
        override_get_session(
            app,
            raises=OperationalError("SELECT 1", {}, Exception("connection refused")),
        )
        override_get_chain_client(app)

        response = await client.get("/health")

        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "down"
        assert body["db"] == "down"
        assert body["chain"] == "ok"


class TestHealthChainDown:
    """Chain unreachable - HTTP 503 with ``chain: down``."""

    async def test_returns_503(self, app: FastAPI, client: httpx.AsyncClient):
        override_get_session(app)
        override_get_chain_client(app, raises=ChainConnectionError("pylon down"))

        response = await client.get("/health")

        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "down"
        assert body["db"] == "ok"
        assert body["chain"] == "down"


class TestHealthBothDown:
    """Both deps unreachable - HTTP 503 with both fields ``down``."""

    async def test_returns_503(self, app: FastAPI, client: httpx.AsyncClient):
        override_get_session(
            app, raises=OperationalError("SELECT 1", {}, Exception("x"))
        )
        override_get_chain_client(app, raises=ChainConnectionError("y"))

        response = await client.get("/health")

        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "down"
        assert body["db"] == "down"
        assert body["chain"] == "down"


class TestHealthSchemaExclusion:
    """``/health`` must not appear in the public OpenAPI schema."""

    async def test_path_not_in_openapi(self, client: httpx.AsyncClient):
        schema = await client.get("/openapi.json")
        assert "/health" not in schema.json()["paths"]
