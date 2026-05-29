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

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(
        _request: Request, _exc: Exception
    ) -> JSONResponse:
        logger.exception("unhandled exception in request handler")
        return _envelope_response(500, ERROR_CODE_UNHANDLED, "internal server error")
