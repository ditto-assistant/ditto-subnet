"""Uniform JSON error envelope for uncaught exceptions."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request

from ditto.api_server.middleware.request_id import (
    REQUEST_ID_HEADER,
    request_id_var,
)
from ditto.api_server.payment_verifier import (
    PaymentAmountMismatch,
    PaymentCallTypeMismatch,
    PaymentDestinationMismatch,
    PaymentExtrinsicFailed,
    PaymentNotFoundOnChain,
    PaymentSignerMismatch,
    PaymentVerifierError,
)
from ditto.api_server.pricing import (
    MalformedPriceError,
    OracleUnreachableError,
    PriceTooStaleError,
    PricingError,
)

logger = logging.getLogger(__name__)

# Platform error codes (3xxx range per CODE-REVIEW-CHECKLIST).
ERROR_CODE_UNHANDLED = 3000
ERROR_CODE_VALIDATION = 3001
ERROR_CODE_HTTP_EXCEPTION = 3002
ERROR_CODE_PRICING = 3100
ERROR_CODE_ORACLE_UNREACHABLE = 3101
ERROR_CODE_MALFORMED_PRICE = 3102
ERROR_CODE_PRICE_TOO_STALE = 3103
ERROR_CODE_PAYMENT_VERIFIER = 3200
ERROR_CODE_PAYMENT_NOT_FOUND = 3201
ERROR_CODE_PAYMENT_EXTRINSIC_FAILED = 3202
ERROR_CODE_PAYMENT_AMOUNT_MISMATCH = 3203
ERROR_CODE_PAYMENT_DESTINATION_MISMATCH = 3204
ERROR_CODE_PAYMENT_SIGNER_MISMATCH = 3205
ERROR_CODE_PAYMENT_CALL_TYPE_MISMATCH = 3206
# 3207 reserved for PaymentReplayedError (db/queries/payments.py,
# feat/upload-agent); pre-allocated so the next PR does not need to
# renumber and so reviewers see the slot is taken.


def _envelope(error_code: int, message: str) -> dict[str, Any]:
    return {
        "error_code": error_code,
        "message": message,
        "request_id": request_id_var.get(),
    }


def _envelope_response(status_code: int, error_code: int, message: str) -> JSONResponse:
    """Build the canonical JSON response with the request id echoed on a header.

    Header is set here as backup so error responses still carry the id
    even on code paths where ``RequestIDMiddleware`` cannot set it.
    """
    rid = request_id_var.get()
    return JSONResponse(
        status_code=status_code,
        content=_envelope(error_code, message),
        headers={REQUEST_ID_HEADER: rid},
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Attach the three envelope handlers to ``app``."""

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(
        _request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        message = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        return _envelope_response(exc.status_code, ERROR_CODE_HTTP_EXCEPTION, message)

    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Full validation details land in server logs; the public body
        # stays generic so user-supplied input never echoes back.
        logger.warning(f"request validation failed: {exc.errors()}")
        return _envelope_response(
            422, ERROR_CODE_VALIDATION, "request validation failed"
        )

    @app.exception_handler(OracleUnreachableError)
    async def _oracle_unreachable_handler(
        _request: Request, exc: OracleUnreachableError
    ) -> JSONResponse:
        logger.warning(f"pricing oracle unreachable: {exc}")
        return _envelope_response(
            503, ERROR_CODE_ORACLE_UNREACHABLE, "pricing oracle unavailable"
        )

    @app.exception_handler(PriceTooStaleError)
    async def _price_too_stale_handler(
        _request: Request, exc: PriceTooStaleError
    ) -> JSONResponse:
        logger.warning(f"pricing cache past max-stale window: {exc}")
        return _envelope_response(
            503, ERROR_CODE_PRICE_TOO_STALE, "pricing oracle unavailable"
        )

    @app.exception_handler(MalformedPriceError)
    async def _malformed_price_handler(
        _request: Request, exc: MalformedPriceError
    ) -> JSONResponse:
        logger.error(f"pricing oracle returned malformed price: {exc}")
        return _envelope_response(
            503, ERROR_CODE_MALFORMED_PRICE, "pricing data is invalid"
        )

    @app.exception_handler(PricingError)
    async def _pricing_error_handler(
        _request: Request, exc: PricingError
    ) -> JSONResponse:
        # Catch-all for any future PricingError subclass that the specific
        # handlers above don't cover.
        logger.warning(f"pricing error: {exc}")
        return _envelope_response(503, ERROR_CODE_PRICING, "pricing failure")

    @app.exception_handler(PaymentNotFoundOnChain)
    async def _payment_not_found_handler(
        _request: Request, exc: PaymentNotFoundOnChain
    ) -> JSONResponse:
        logger.info(f"payment not found on chain: {exc}")
        return _envelope_response(
            402, ERROR_CODE_PAYMENT_NOT_FOUND, "payment extrinsic not found on chain"
        )

    @app.exception_handler(PaymentExtrinsicFailed)
    async def _payment_extrinsic_failed_handler(
        _request: Request, exc: PaymentExtrinsicFailed
    ) -> JSONResponse:
        logger.info(f"payment extrinsic failed: {exc}")
        return _envelope_response(
            402,
            ERROR_CODE_PAYMENT_EXTRINSIC_FAILED,
            "payment extrinsic failed on chain",
        )

    @app.exception_handler(PaymentAmountMismatch)
    async def _payment_amount_mismatch_handler(
        _request: Request, exc: PaymentAmountMismatch
    ) -> JSONResponse:
        logger.info(f"payment amount mismatch: {exc}")
        return _envelope_response(
            402, ERROR_CODE_PAYMENT_AMOUNT_MISMATCH, "payment amount mismatch"
        )

    @app.exception_handler(PaymentDestinationMismatch)
    async def _payment_destination_mismatch_handler(
        _request: Request, exc: PaymentDestinationMismatch
    ) -> JSONResponse:
        logger.info(f"payment destination mismatch: {exc}")
        return _envelope_response(
            402,
            ERROR_CODE_PAYMENT_DESTINATION_MISMATCH,
            "payment destination mismatch",
        )

    @app.exception_handler(PaymentSignerMismatch)
    async def _payment_signer_mismatch_handler(
        _request: Request, exc: PaymentSignerMismatch
    ) -> JSONResponse:
        logger.info(f"payment signer mismatch: {exc}")
        return _envelope_response(
            402, ERROR_CODE_PAYMENT_SIGNER_MISMATCH, "payment signer mismatch"
        )

    @app.exception_handler(PaymentCallTypeMismatch)
    async def _payment_call_type_mismatch_handler(
        _request: Request, exc: PaymentCallTypeMismatch
    ) -> JSONResponse:
        logger.info(f"payment call type mismatch: {exc}")
        return _envelope_response(
            402,
            ERROR_CODE_PAYMENT_CALL_TYPE_MISMATCH,
            "payment call type mismatch",
        )

    @app.exception_handler(PaymentVerifierError)
    async def _payment_verifier_error_handler(
        _request: Request, exc: PaymentVerifierError
    ) -> JSONResponse:
        # Catch-all for any future PaymentVerifierError subclass that the
        # specific handlers above don't cover.
        logger.warning(f"payment verifier error: {exc}")
        return _envelope_response(
            402, ERROR_CODE_PAYMENT_VERIFIER, "payment verification failed"
        )

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(
        _request: Request, _exc: Exception
    ) -> JSONResponse:
        logger.exception("unhandled exception in request handler")
        return _envelope_response(500, ERROR_CODE_UNHANDLED, "internal server error")
