"""Unit tests for :mod:`ditto.api_server.endpoints.health`."""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.exc import OperationalError

from ditto.api_server import revision
from ditto.chain.errors import ChainConnectionError
from ditto.tests.api_server.conftest import (
    override_get_chain_client,
    override_get_session,
)


@pytest.fixture(autouse=True)
def _clear_revision_cache():
    """``checked_out_commit`` memoises; keep probes independent across tests."""
    revision.reset_cache()
    yield
    revision.reset_cache()


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


class TestHealthRevisionDrift:
    """Is the running process on the checked-out revision?

    The 2026-07-25 near-outage was a host with new code checked out and an
    hour-old process serving it, indistinguishable from a healthy deploy at
    the git layer. ``/health`` now reports both revisions and says whether
    they agree.
    """

    async def test_reports_the_checked_out_commit_separately(
        self, app: FastAPI, client: httpx.AsyncClient
    ):
        override_get_session(app)
        override_get_chain_client(app)
        app.state.commit_hash = "aaa111"

        with patch(
            "ditto.api_server.revision.resolve_commit_hash", return_value="aaa111"
        ):
            response = await client.get("/health")

        body = response.json()
        assert body["commit"] == "aaa111"
        assert body["checked_out_commit"] == "aaa111"
        assert body["commit_drift"] is False

    async def test_flags_drift_when_the_checkout_moved_past_the_process(
        self, app: FastAPI, client: httpx.AsyncClient
    ):
        override_get_session(app)
        override_get_chain_client(app)
        app.state.commit_hash = "aaa111"

        with patch(
            "ditto.api_server.revision.resolve_commit_hash", return_value="bbb222"
        ):
            response = await client.get("/health")

        body = response.json()
        assert body["commit"] == "aaa111"
        assert body["checked_out_commit"] == "bbb222"
        assert body["commit_drift"] is True

    async def test_drift_does_not_degrade_the_probe(
        self, app: FastAPI, client: httpx.AsyncClient
    ):
        """Stale code is a deploy problem, not a reason to fail the instance.

        A 503 here would pull a serving host out of rotation over a deploy
        bookkeeping mismatch, turning stale code into an outage.
        """
        override_get_session(app)
        override_get_chain_client(app)
        app.state.commit_hash = "aaa111"

        with patch(
            "ditto.api_server.revision.resolve_commit_hash", return_value="bbb222"
        ):
            response = await client.get("/health")

        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    async def test_unknown_revision_is_not_reported_as_drift(
        self, app: FastAPI, client: httpx.AsyncClient
    ):
        """A checkout without git history must not fail every deploy."""
        override_get_session(app)
        override_get_chain_client(app)
        app.state.commit_hash = "aaa111"

        with patch(
            "ditto.api_server.revision.resolve_commit_hash", return_value="unknown"
        ):
            response = await client.get("/health")

        body = response.json()
        assert body["checked_out_commit"] == "unknown"
        assert body["commit_drift"] is False


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
