"""Uniform JSON error envelope for every uncaught exception.

Implemented as FastAPI exception handlers rather than middleware: handlers
run before response bytes flush, so they can transform an in-flight error
without needing to wrap the ASGI send channel. The catch-all chain is:

1. :class:`starlette.exceptions.HTTPException` - preserves the original
   status code, wraps the detail in the envelope.
2. :class:`fastapi.exceptions.RequestValidationError` - 422 in the same
   envelope (FastAPI's default uses a different shape).
3. :class:`Exception` - 500 catch-all with a 3000-range platform error code.

Envelope shape:

.. code-block:: json

    {"error_code": 3000, "message": "...", "request_id": "..."}

The ``error_code`` ranges (1xxx agent / 2xxx validator / 3xxx platform)
are documented in the review checklist. This module only registers the
3xxx ``UNHANDLED`` and ``VALIDATION`` codes; per-endpoint codes land
with their endpoint PRs.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request

from ditto.api_server.middleware.request_id import request_id_var

logger = logging.getLogger(__name__)

# Platform error codes (3xxx range).
ERROR_CODE_UNHANDLED = 3000
ERROR_CODE_VALIDATION = 3001
ERROR_CODE_HTTP_EXCEPTION = 3002


def _envelope(error_code: int, message: str) -> dict[str, Any]:
    """Build the canonical error JSON body."""
    return {
        "error_code": error_code,
        "message": message,
        "request_id": request_id_var.get(),
    }


def register_exception_handlers(app: FastAPI) -> None:
    """Attach the three uniform-envelope handlers to ``app``.

    Called from :func:`ditto.api_server.factory.create_api_server` after
    middleware is registered. Idempotent: re-registering the same handler
    type replaces the previous registration.
    """

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(
        _request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        message = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        return JSONResponse(
            status_code=exc.status_code,
            content=_envelope(ERROR_CODE_HTTP_EXCEPTION, message),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content=_envelope(
                ERROR_CODE_VALIDATION,
                f"request validation failed: {exc.errors()}",
            ),
        )

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(
        _request: Request, _exc: Exception
    ) -> JSONResponse:
        logger.exception("unhandled exception in request handler")
        return JSONResponse(
            status_code=500,
            content=_envelope(ERROR_CODE_UNHANDLED, "internal server error"),
        )
