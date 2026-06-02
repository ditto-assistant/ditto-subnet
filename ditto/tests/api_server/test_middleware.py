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
    ERROR_CODE_PAYMENT_AMOUNT_MISMATCH,
    ERROR_CODE_PAYMENT_CALL_TYPE_MISMATCH,
    ERROR_CODE_PAYMENT_DESTINATION_MISMATCH,
    ERROR_CODE_PAYMENT_EXTRINSIC_FAILED,
    ERROR_CODE_PAYMENT_NOT_FOUND,
    ERROR_CODE_PAYMENT_REPLAYED,
    ERROR_CODE_PAYMENT_SIGNER_MISMATCH,
    ERROR_CODE_PAYMENT_VERIFIER,
    ERROR_CODE_UNHANDLED,
    ERROR_CODE_VALIDATION,
)
from ditto.api_server.middleware.request_id import (
    REQUEST_ID_HEADER,
    RequestIdFilter,
    request_id_var,
)
from ditto.api_server.payment_verifier import (
    PaymentAmountMismatch,
    PaymentCallTypeMismatch,
    PaymentDestinationMismatch,
    PaymentExtrinsicFailed,
    PaymentNotFoundOnChain,
    PaymentReplayedError,
    PaymentSignerMismatch,
    PaymentVerifierError,
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

    async def test_malicious_request_id_is_rejected(
        self, app: FastAPI, client: httpx.AsyncClient
    ):
        """Inbound X-Request-ID values with control chars or excessive
        length must be replaced with a fresh UUID."""
        override_get_session(app)
        override_get_chain_client(app)

        # Newline injection attempt - would forge log lines if accepted.
        response = await client.get(
            "/health", headers={REQUEST_ID_HEADER: "abc\n[FAKE LOG]"}
        )
        echoed = response.headers[REQUEST_ID_HEADER]
        assert echoed != "abc\n[FAKE LOG]"
        # UUID4 hex is exactly 32 chars; the fallback should have produced one.
        assert len(echoed) == 32

        # Cardinality blow-up: 200-char id rejected.
        long_id = "x" * 200
        response = await client.get("/health", headers={REQUEST_ID_HEADER: long_id})
        assert response.headers[REQUEST_ID_HEADER] != long_id
        assert len(response.headers[REQUEST_ID_HEADER]) == 32

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


class TestPaymentVerifierEnvelope:
    """Each PaymentVerifierError subclass surfaces a typed 402 envelope."""

    @pytest.mark.parametrize(
        ("exc", "expected_code"),
        [
            (PaymentNotFoundOnChain("nope"), ERROR_CODE_PAYMENT_NOT_FOUND),
            (PaymentExtrinsicFailed("failed"), ERROR_CODE_PAYMENT_EXTRINSIC_FAILED),
            (PaymentAmountMismatch("band"), ERROR_CODE_PAYMENT_AMOUNT_MISMATCH),
            (
                PaymentDestinationMismatch("dest"),
                ERROR_CODE_PAYMENT_DESTINATION_MISMATCH,
            ),
            (PaymentSignerMismatch("signer"), ERROR_CODE_PAYMENT_SIGNER_MISMATCH),
            (PaymentCallTypeMismatch("call"), ERROR_CODE_PAYMENT_CALL_TYPE_MISMATCH),
            (PaymentReplayedError("replay"), ERROR_CODE_PAYMENT_REPLAYED),
        ],
    )
    async def test_specific_handlers_map_to_402(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        exc: Exception,
        expected_code: int,
    ):
        """One throw-route per error type so each handler is exercised."""

        @app.get("/_test/payment_specific")
        async def _raise() -> dict[str, Any]:
            raise exc

        response = await client.get("/_test/payment_specific")
        assert response.status_code == 402
        body = response.json()
        assert body["error_code"] == expected_code
        assert "request_id" in body

    async def test_base_class_catch_all_handler(
        self, app: FastAPI, client: httpx.AsyncClient
    ):
        """Future PaymentVerifierError subclasses without dedicated
        handlers must still surface as the base-class envelope."""

        class _Custom(PaymentVerifierError):
            pass

        @app.get("/_test/payment_base")
        async def _raise() -> dict[str, Any]:
            raise _Custom("unmapped subclass")

        response = await client.get("/_test/payment_base")
        assert response.status_code == 402
        body = response.json()
        assert body["error_code"] == ERROR_CODE_PAYMENT_VERIFIER

    async def test_replay_does_not_fall_through_to_catch_all(
        self, app: FastAPI, client: httpx.AsyncClient
    ):
        """Without the specific 3207 handler the replay would surface as
        the generic 3200 fallback. Pin the ordering by asserting the code."""

        @app.get("/_test/payment_replay")
        async def _raise() -> dict[str, Any]:
            raise PaymentReplayedError("seen before")

        response = await client.get("/_test/payment_replay")
        assert response.status_code == 402
        body = response.json()
        assert body["error_code"] == ERROR_CODE_PAYMENT_REPLAYED
        assert body["error_code"] != ERROR_CODE_PAYMENT_VERIFIER
        assert body["message"] == "payment proof already used"


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
