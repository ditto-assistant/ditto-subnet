"""Unit tests for :mod:`ditto.api_server.middleware`."""

from __future__ import annotations

import logging
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError

from ditto.api_server.endpoints.inference import InferenceDeclinedError
from ditto.api_server.middleware.error_envelope import (
    ERROR_CODE_HTTP_EXCEPTION,
    ERROR_CODE_INFERENCE_AT_CAPACITY,
    ERROR_CODE_INFERENCE_BUDGET_EXHAUSTED,
    ERROR_CODE_INFERENCE_DECLINED,
    ERROR_CODE_INFERENCE_GRANT_NOT_EXCHANGED,
    ERROR_CODE_INFERENCE_GRANT_REVOKED,
    ERROR_CODE_INFERENCE_LEASE_EXPIRED,
    ERROR_CODE_INFERENCE_MODEL_NOT_PERMITTED,
    ERROR_CODE_INFERENCE_NONCE_REPLAYED,
    ERROR_CODE_INFERENCE_RESERVATION_TOO_LARGE,
    ERROR_CODE_INFERENCE_TOKEN_BUDGET_EXHAUSTED,
    ERROR_CODE_PAYMENT_AMOUNT_MISMATCH,
    ERROR_CODE_PAYMENT_CALL_TYPE_MISMATCH,
    ERROR_CODE_PAYMENT_DESTINATION_MISMATCH,
    ERROR_CODE_PAYMENT_EXTRINSIC_FAILED,
    ERROR_CODE_PAYMENT_NOT_FOUND,
    ERROR_CODE_PAYMENT_RECOVERY_EXPIRED,
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
    PaymentRecoveryExpired,
    PaymentReplayedError,
    PaymentSignerMismatch,
    PaymentVerifierError,
)
from ditto.db.queries.inference import InferenceDecline
from ditto.tests.api_server.conftest import (
    override_get_chain_client,
    override_get_session,
)


def _attach_error_routes(app: FastAPI) -> None:
    """Add three throw-routes covering each exception-handler path."""

    @app.get("/_test/http")
    async def _raise_http() -> dict[str, Any]:
        raise HTTPException(
            status_code=418,
            detail="i am a teapot",
            headers={"Retry-After": "60"},
        )

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
        assert response.headers["Retry-After"] == "60"

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
                PaymentRecoveryExpired("expired"),
                ERROR_CODE_PAYMENT_RECOVERY_EXPIRED,
            ),
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


class TestInferenceDeclineEnvelope:
    """The wire contract a broker classifies on.

    This is the whole point of the 429 split, so it is asserted at the level a
    broker actually observes: status, ``Retry-After``, and the numeric code in
    the body. Asserting it on the enum instead would pass while the thing that
    reaches the fleet was wrong.
    """

    @pytest.mark.parametrize(
        ("decline", "expected_status", "expected_code"),
        [
            (
                InferenceDecline.AT_CAPACITY,
                503,
                ERROR_CODE_INFERENCE_AT_CAPACITY,
            ),
            (
                InferenceDecline.GRANT_REVOKED,
                429,
                ERROR_CODE_INFERENCE_GRANT_REVOKED,
            ),
            (
                InferenceDecline.BUDGET_EXHAUSTED,
                429,
                ERROR_CODE_INFERENCE_BUDGET_EXHAUSTED,
            ),
            (
                InferenceDecline.TOKEN_BUDGET_EXHAUSTED,
                429,
                ERROR_CODE_INFERENCE_TOKEN_BUDGET_EXHAUSTED,
            ),
            (
                InferenceDecline.LEASE_EXPIRED,
                429,
                ERROR_CODE_INFERENCE_LEASE_EXPIRED,
            ),
            (
                InferenceDecline.NONCE_REPLAYED,
                429,
                ERROR_CODE_INFERENCE_NONCE_REPLAYED,
            ),
            (
                InferenceDecline.MODEL_NOT_PERMITTED,
                429,
                ERROR_CODE_INFERENCE_MODEL_NOT_PERMITTED,
            ),
            (
                InferenceDecline.GRANT_NOT_EXCHANGED,
                429,
                ERROR_CODE_INFERENCE_GRANT_NOT_EXCHANGED,
            ),
            (
                InferenceDecline.RESERVATION_TOO_LARGE,
                429,
                ERROR_CODE_INFERENCE_RESERVATION_TOO_LARGE,
            ),
            (InferenceDecline.UNATTRIBUTED, 429, ERROR_CODE_INFERENCE_DECLINED),
        ],
    )
    async def test_each_decline_has_its_own_status_and_code(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        decline: InferenceDecline,
        expected_status: int,
        expected_code: int,
    ):
        @app.get("/_test/inference_decline")
        async def _raise() -> dict[str, Any]:
            raise InferenceDeclinedError(decline, lane="inference")

        response = await client.get("/_test/inference_decline")
        assert response.status_code == expected_status
        assert response.json()["error_code"] == expected_code

    async def test_no_decline_falls_through_to_the_anonymous_code(
        self, app: FastAPI, client: httpx.AsyncClient
    ):
        """Every member is mapped, and only the deliberate one answers 4100.

        This is the regression guard for the shape of bug that cost 1009
        requests on a live lease: a decline exists in the enum, nothing maps it,
        and it silently degrades to the anonymous refusal that the broker reads
        as transient. Walking the enum here means adding a member without
        deciding its code fails the suite rather than a run.
        """
        for member in InferenceDecline:

            @app.get(f"/_test/sweep/{member.value}")
            async def _raise(member: InferenceDecline = member) -> dict[str, Any]:
                raise InferenceDeclinedError(member, lane="inference")

            response = await client.get(f"/_test/sweep/{member.value}")
            code = response.json()["error_code"]
            if member is InferenceDecline.UNATTRIBUTED:
                assert code == ERROR_CODE_INFERENCE_DECLINED
                # ...and it says that the silence is a choice.
                assert "deliberately not disclosed" in response.json()["message"]
            else:
                assert code != ERROR_CODE_INFERENCE_DECLINED, (
                    f"{member} falls through to the anonymous 4100"
                )
            assert response.status_code in {429, 503}

    async def test_only_the_retryable_decline_carries_retry_after(
        self, app: FastAPI, client: httpx.AsyncClient
    ):
        """``Retry-After`` is the half of the signal old brokers can read.

        A build that predates the error codes classifies on status alone, and
        the embedding lane taught it that ``503`` + ``Retry-After`` means "back
        off and come back". Putting the header on a terminal decline would
        invite exactly the retry loop against a dead grant that this design
        exists to prevent.
        """

        @app.get("/_test/decline_capacity")
        async def _capacity() -> dict[str, Any]:
            raise InferenceDeclinedError(InferenceDecline.AT_CAPACITY, lane="embedding")

        @app.get("/_test/decline_revoked")
        async def _revoked() -> dict[str, Any]:
            raise InferenceDeclinedError(
                InferenceDecline.GRANT_REVOKED, lane="embedding"
            )

        capacity = await client.get("/_test/decline_capacity")
        assert capacity.status_code == 503
        assert capacity.headers["Retry-After"] == "1"

        revoked = await client.get("/_test/decline_revoked")
        assert revoked.status_code == 429
        assert "Retry-After" not in revoked.headers

    async def test_the_two_terminal_declines_share_a_status_but_not_a_code(
        self, app: FastAPI, client: httpx.AsyncClient
    ):
        """Revocation and exhaustion stay on 429 *deliberately*.

        An old broker already treats 429 as fatal, which is correct for both,
        so neither may move to a status it would mishandle. What they must not
        share is the code -- that is the only thing letting a new broker
        discard a dead run but wind down a merely-spent one.
        """

        @app.get("/_test/decline_exhausted")
        async def _exhausted() -> dict[str, Any]:
            raise InferenceDeclinedError(
                InferenceDecline.BUDGET_EXHAUSTED, lane="inference"
            )

        @app.get("/_test/decline_dead")
        async def _dead() -> dict[str, Any]:
            raise InferenceDeclinedError(
                InferenceDecline.GRANT_REVOKED, lane="inference"
            )

        exhausted = await client.get("/_test/decline_exhausted")
        dead = await client.get("/_test/decline_dead")
        assert exhausted.status_code == dead.status_code == 429
        assert exhausted.json()["error_code"] != dead.json()["error_code"]
