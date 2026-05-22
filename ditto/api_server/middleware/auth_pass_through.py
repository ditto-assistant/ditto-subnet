"""No-op auth middleware stub.

Position-locked so the future swap to real auth is a single body change.
When the first authenticated endpoint lands, this file will:

- Read the ``Authorization`` header, extract the session UUID bearer
- Look up the matching ``validator_sessions`` row via the request's
  session maker
- Attach the session record to ``request.state.session`` for downstream
  handlers
- Return 401 on missing / expired tokens

Until then it forwards every request unchanged so endpoints can be
exercised without auth gymnastics.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.middleware.base import BaseHTTPMiddleware

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response


class AuthPassThroughMiddleware(BaseHTTPMiddleware):
    """Forward each request to the next handler unchanged.

    Real implementation lands with the validator-session endpoints in a
    later PR. Keeping the class registered now locks the middleware
    order so the swap is a one-line change to the dispatch body.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        return await call_next(request)
