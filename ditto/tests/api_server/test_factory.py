"""Unit tests for :mod:`ditto.api_server.factory`."""

from __future__ import annotations

from fastapi import FastAPI

from ditto.api_server import create_api_server
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
