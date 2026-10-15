"""Keep private responses out of every cache, errors included."""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import Response

NO_STORE = "no-store"


def no_store(request: Request, response: Response) -> None:
    """Router dependency for private routes.

    The header on ``response`` covers a successful result. Exception handlers
    build error responses separately, so the request is also marked for
    :func:`mark_error_no_store`.
    """
    response.headers["Cache-Control"] = NO_STORE
    request.state.no_store = True


def mark_error_no_store(request: Request, response: Response | None) -> None:
    """Apply a private route's cache policy to its error response."""
    if response is not None and getattr(request.state, "no_store", False):
        response.headers["Cache-Control"] = NO_STORE
