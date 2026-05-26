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

logger = logging.getLogger(__name__)

# Platform error codes (3xxx range per CODE-REVIEW-CHECKLIST).
ERROR_CODE_UNHANDLED = 3000
ERROR_CODE_VALIDATION = 3001
ERROR_CODE_HTTP_EXCEPTION = 3002


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
        # TODO(security): exc.errors() can echo raw user input in the
        # body. Acceptable while the API has no HTML consumers; revisit
        # before the public dashboard lands.
        return _envelope_response(
            422, ERROR_CODE_VALIDATION, f"request validation failed: {exc.errors()}"
        )

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(
        _request: Request, _exc: Exception
    ) -> JSONResponse:
        logger.exception("unhandled exception in request handler")
        return _envelope_response(500, ERROR_CODE_UNHANDLED, "internal server error")
