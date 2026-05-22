"""Request-id correlation across the request scope.

``contextvars.ContextVar`` propagates across ``await`` boundaries (PEP 567)
which lets stdlib :mod:`logging` records carry the request id without
the call sites needing a :class:`logging.LoggerAdapter`. The middleware
sets the contextvar on entry and resets it on exit; a filter copies the
current value onto each log record so the formatter's ``%(request_id)s``
placeholder always resolves.

The same middleware emits the one-line access log (replacing
``uvicorn.access``, which the dictConfig disables) so latency + status
appear under the same request id as everything else.
"""

from __future__ import annotations

import logging
import time
from contextvars import ContextVar
from typing import TYPE_CHECKING
from uuid import uuid4

from starlette.middleware.base import BaseHTTPMiddleware

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.types import ASGIApp

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"
"""Header read on incoming requests and echoed on outgoing responses."""

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")
"""Per-task request id. Default ``"-"`` so records logged outside a
request scope (lifespan, background tasks) still format cleanly."""


class RequestIdFilter(logging.Filter):
    """Stuff the current request id onto every log record.

    Referenced from :func:`ditto.api_server.logging_config.build_dict_config`
    via its dotted path; instantiated by :func:`logging.config.dictConfig`.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class RequestIDMiddleware(BaseHTTPMiddleware):
    """ASGI middleware that anchors the per-request correlation id.

    - Reads ``X-Request-ID`` from the incoming request; generates a fresh
      hex UUID when absent.
    - Sets the ``request_id_var`` contextvar and stores the value on
      ``request.state.request_id``.
    - Adds the same value to the outgoing response header so callers can
      thread it through their own log lines.
    - Emits the access-log line after ``call_next`` (replaces the
      ``uvicorn.access`` logger, disabled in the dictConfig).
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next) -> Response:
        rid = request.headers.get(REQUEST_ID_HEADER) or uuid4().hex
        token = request_id_var.set(rid)
        request.state.request_id = rid
        start = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            status = getattr(locals().get("response", None), "status_code", "ERR")
            logger.info(
                f"{request.method} {request.url.path} -> {status} in {elapsed_ms:.1f}ms"
            )
            request_id_var.reset(token)
        response.headers[REQUEST_ID_HEADER] = rid
        return response
