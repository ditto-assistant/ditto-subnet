"""Request-id correlation across the request scope.

``contextvars.ContextVar`` propagates across ``await`` boundaries so
stdlib :mod:`logging` records carry the request id without callers
reaching for :class:`logging.LoggerAdapter`. A filter copies the
current value onto every log record so ``%(request_id)s`` resolves.
"""

from __future__ import annotations

import logging
import re
import time
from contextvars import ContextVar
from typing import TYPE_CHECKING
from uuid import uuid4

from starlette.middleware.base import BaseHTTPMiddleware

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"

# Accept only alphanumerics + `_-.` up to 64 chars on inbound headers so
# log lines cannot be forged (newline injection) and cardinality cannot
# be blown up by attackers crafting huge unique values.
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


class RequestIdFilter(logging.Filter):
    """Copy the active request id onto every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class RequestIDMiddleware(BaseHTTPMiddleware):
    """ASGI middleware that anchors a per-request correlation id.

    Reads ``X-Request-ID`` from the incoming request or generates a
    fresh hex UUID. Sets the contextvar, attaches the id to
    ``request.state``, echoes it on the response header, and emits a
    single access-log line so ``uvicorn.access`` (disabled in the
    dictConfig) does not double-log.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        incoming = request.headers.get(REQUEST_ID_HEADER, "")
        rid = incoming if _SAFE_REQUEST_ID.fullmatch(incoming) else uuid4().hex
        token = request_id_var.set(rid)
        request.state.request_id = rid
        start = time.perf_counter()
        response: Response | None = None
        try:
            response = await call_next(request)
            return response
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            status = response.status_code if response is not None else "ERR"
            logger.info(
                f"{request.method} {request.url.path} -> {status} in {elapsed_ms:.1f}ms"
            )
            if response is not None:
                response.headers[REQUEST_ID_HEADER] = rid
            request_id_var.reset(token)
