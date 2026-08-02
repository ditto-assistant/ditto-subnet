"""Placeholder auth middleware. Forwards every request unchanged until
real session-token validation lands with the validator endpoints."""

from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.middleware.base import BaseHTTPMiddleware

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response


class AuthPassThroughMiddleware(BaseHTTPMiddleware):
    """Forward each request unchanged."""

    async def dispatch(self, request: Request, call_next) -> Response:
        return await call_next(request)
