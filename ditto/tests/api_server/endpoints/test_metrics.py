"""Unit tests for :mod:`ditto.api_server.endpoints.metrics`."""

from __future__ import annotations

import httpx


class TestMetrics:
    """Prometheus exposition shape."""

    async def test_returns_200(self, client: httpx.AsyncClient):
        response = await client.get("/metrics")
        assert response.status_code == 200

    async def test_content_type_is_prometheus_exposition(
        self, client: httpx.AsyncClient
    ):
        response = await client.get("/metrics")
        # CONTENT_TYPE_LATEST is "text/plain; version=0.0.4; charset=utf-8".
        assert response.headers["content-type"].startswith("text/plain")

    async def test_body_contains_help_or_type_lines(self, client: httpx.AsyncClient):
        """Default registry exposes process collectors so body should
        carry at least one ``# HELP`` or ``# TYPE`` directive."""
        response = await client.get("/metrics")
        body = response.text
        assert "# HELP" in body or "# TYPE" in body

    async def test_path_not_in_openapi(self, client: httpx.AsyncClient):
        schema = await client.get("/openapi.json")
        assert "/metrics" not in schema.json()["paths"]
