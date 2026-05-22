"""Unit tests for :mod:`ditto.api_server.factory`."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI

from ditto.api_server import create_api_server
from ditto.api_server.errors import ApiServerLifespanError
from ditto.api_server.middleware import (
    AuthPassThroughMiddleware,
    RequestIDMiddleware,
)
from ditto.tests.api_server.conftest import make_api_server_config


class TestCreateApiServer:
    """Sanity-checks the wiring."""

    def test_returns_fastapi_instance(self):
        app = create_api_server(make_api_server_config())
        assert isinstance(app, FastAPI)

    def test_state_carries_config(self):
        config = make_api_server_config()
        app = create_api_server(config)
        assert app.state.config is config
        assert app.state.commit_hash == "test-commit"

    def test_middleware_stack_registered(self):
        app = create_api_server(make_api_server_config())
        classes = {m.cls for m in app.user_middleware}
        assert RequestIDMiddleware in classes
        assert AuthPassThroughMiddleware in classes

    def test_redoc_disabled(self):
        app = create_api_server(make_api_server_config())
        assert app.redoc_url is None
        assert app.docs_url == "/docs"
        assert app.openapi_url == "/openapi.json"

    def test_health_and_metrics_excluded_from_schema(self):
        app = create_api_server(make_api_server_config())
        schema = app.openapi()
        # Ops routes have include_in_schema=False so they should not appear.
        assert "/health" not in schema["paths"]
        assert "/metrics" not in schema["paths"]

    def test_health_and_metrics_routes_registered(self):
        app = create_api_server(make_api_server_config())
        paths = {route.path for route in app.routes if hasattr(route, "path")}
        assert "/health" in paths
        assert "/metrics" in paths


class TestLifespanFailureCleanup:
    """``AsyncExitStack`` must dispose the engine if chain open fails."""

    async def test_engine_disposed_when_chain_open_raises(self):
        """Regression net for the ordering in ``_make_lifespan``: the
        engine must be registered as a stack callback BEFORE the chain
        client enters, so a chain-open failure unwinds the engine cleanly
        instead of leaking pooled Postgres connections."""
        engine = MagicMock()
        engine.dispose = AsyncMock()

        # The chain client's ``__aenter__`` raises during lifespan startup.
        chain_ctx = MagicMock()
        chain_ctx.__aenter__ = AsyncMock(side_effect=RuntimeError("pylon down"))
        chain_ctx.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("ditto.api_server.factory.create_db_engine", return_value=engine),
            patch(
                "ditto.api_server.factory.create_session_maker",
                return_value=MagicMock(),
            ),
            patch(
                "ditto.api_server.factory.create_chain_client",
                return_value=chain_ctx,
            ),
        ):
            app = create_api_server(make_api_server_config())
            with pytest.raises(ApiServerLifespanError, match="pylon down"):
                async with app.router.lifespan_context(app):
                    pass

        engine.dispose.assert_awaited_once()
