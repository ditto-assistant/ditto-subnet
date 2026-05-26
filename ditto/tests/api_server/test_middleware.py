"""Unit tests for :mod:`ditto.api_server.middleware`."""

from __future__ import annotations

import logging
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError

from ditto.api_server.middleware.error_envelope import (
    ERROR_CODE_HTTP_EXCEPTION,
    ERROR_CODE_UNHANDLED,
    ERROR_CODE_VALIDATION,
)
from ditto.api_server.middleware.request_id import (
    REQUEST_ID_HEADER,
    RequestIdFilter,
    request_id_var,
)
from ditto.tests.api_server.conftest import (
    override_get_chain_client,
    override_get_session,
)


def _attach_error_routes(app: FastAPI) -> None:
    """Add three throw-routes covering each exception-handler path."""

    @app.get("/_test/http")
    async def _raise_http() -> dict[str, Any]:
        raise HTTPException(status_code=418, detail="i am a teapot")

    @app.get("/_test/validation")
    async def _raise_validation() -> dict[str, Any]:
        raise RequestValidationError(errors=[{"msg": "bad"}])

    @app.get("/_test/unhandled")
    async def _raise_unhandled() -> dict[str, Any]:
        raise RuntimeError("boom")


class TestRequestIDMiddleware:
    """Request-id correlation behaviour."""

    async def test_header_preserved_when_provided(
        self, app: FastAPI, client: httpx.AsyncClient
    ):
        override_get_session(app)
        override_get_chain_client(app)
        rid = "test-rid-12345"
        response = await client.get("/health", headers={REQUEST_ID_HEADER: rid})
        assert response.headers[REQUEST_ID_HEADER] == rid

    async def test_header_generated_when_absent(
        self, app: FastAPI, client: httpx.AsyncClient
    ):
        override_get_session(app)
        override_get_chain_client(app)
        response = await client.get("/health")
        assert REQUEST_ID_HEADER in response.headers
        assert len(response.headers[REQUEST_ID_HEADER]) >= 16

    async def test_contextvar_propagates_to_log_records(
        self, app: FastAPI, client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
    ):
        override_get_session(app)
        override_get_chain_client(app)

        # Attach the production filter so the request_id field lands on records.
        handler_filter = RequestIdFilter()
        caplog_handler = caplog.handler
        caplog_handler.addFilter(handler_filter)
        try:
            with caplog.at_level(logging.INFO, logger="ditto.api_server"):
                rid = "test-rid-prop"
                await client.get("/health", headers={REQUEST_ID_HEADER: rid})
            access_records = [
                r for r in caplog.records if "/health -> " in r.getMessage()
            ]
            assert access_records, (
                "request-id middleware should emit an access log line"
            )
            assert all(getattr(r, "request_id", None) == rid for r in access_records)
        finally:
            caplog_handler.removeFilter(handler_filter)

    def test_filter_uses_contextvar_default_outside_request(self):
        """Records logged outside a request scope still format cleanly."""
        # Reset to the default by clearing any leaked value from prior tests.
        token = request_id_var.set("-")
        try:
            record = logging.LogRecord(
                name="x",
                level=20,
                pathname="",
                lineno=0,
                msg="msg",
                args=None,
                exc_info=None,
            )
            RequestIdFilter().filter(record)
            assert record.request_id == "-"
        finally:
            request_id_var.reset(token)

    async def test_contextvar_reset_when_call_next_raises(self):
        """``finally`` must reset the contextvar even when the inner
        ASGI app raises. Tested directly against the middleware (not via
        httpx) so we observe the contextvar in the same task that ran
        the dispatch."""
        from unittest.mock import MagicMock

        from ditto.api_server.middleware.request_id import RequestIDMiddleware

        # Ensure a clean baseline value before we start.
        token = request_id_var.set("-")
        try:
            middleware = RequestIDMiddleware(app=MagicMock())
            request = MagicMock()
            request.headers = {}
            request.method = "GET"
            request.url.path = "/boom"
            request.state = MagicMock()

            async def _raises(_req):
                # Confirm the contextvar IS set during the inner call.
                assert request_id_var.get() != "-"
                raise RuntimeError("inner boom")

            with pytest.raises(RuntimeError, match="inner boom"):
                await middleware.dispatch(request, _raises)

            # finally block must have reset the contextvar.
            assert request_id_var.get() == "-"
        finally:
            request_id_var.reset(token)


class TestErrorEnvelope:
    """Each FastAPI exception handler returns the documented envelope."""

    async def test_http_exception_returns_envelope(
        self, app: FastAPI, client: httpx.AsyncClient
    ):
        _attach_error_routes(app)
        response = await client.get("/_test/http")
        assert response.status_code == 418
        body = response.json()
        assert body["error_code"] == ERROR_CODE_HTTP_EXCEPTION
        assert body["message"] == "i am a teapot"
        assert "request_id" in body

    async def test_validation_error_returns_envelope(
        self, app: FastAPI, client: httpx.AsyncClient
    ):
        _attach_error_routes(app)
        response = await client.get("/_test/validation")
        assert response.status_code == 422
        body = response.json()
        assert body["error_code"] == ERROR_CODE_VALIDATION
        assert "validation failed" in body["message"]
        assert "request_id" in body

    async def test_unhandled_exception_returns_500_envelope(
        self, app: FastAPI, client: httpx.AsyncClient
    ):
        _attach_error_routes(app)
        response = await client.get("/_test/unhandled")
        assert response.status_code == 500
        body = response.json()
        assert body["error_code"] == ERROR_CODE_UNHANDLED
        assert body["message"] == "internal server error"
        assert "request_id" in body

    async def test_envelope_request_id_matches_middleware(
        self, app: FastAPI, client: httpx.AsyncClient
    ):
        _attach_error_routes(app)
        rid = "envelope-rid"
        response = await client.get("/_test/http", headers={REQUEST_ID_HEADER: rid})
        assert response.json()["request_id"] == rid


class TestAuthPassThrough:
    """The no-op stub must not alter responses."""

    async def test_does_not_change_status_or_headers(
        self, app: FastAPI, client: httpx.AsyncClient
    ):
        override_get_session(app)
        override_get_chain_client(app)
        response = await client.get("/health")
        # Stub is transparent: real auth would 401 here.
        assert response.status_code == 200
