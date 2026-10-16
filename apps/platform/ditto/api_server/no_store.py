"""Keep private responses out of every cache, errors included."""

from __future__ import annotations

from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

NO_STORE = "no-store"


def no_store(request: Request, response: Response) -> None:
    """Router dependency for private routes.

    The header on ``response`` covers a value the endpoint returns. A returned
    ``Response`` is built separately, so the request is also marked for
    :class:`NoStoreMiddleware`. Error responses are no-store by default in
    ``envelope_response``, so they need no mark (and a request that fails
    before dependencies run, such as an unparseable body, is still covered).
    """
    response.headers["Cache-Control"] = NO_STORE
    request.state.no_store = True


class NoStoreMiddleware:
    """Add no-store to a marked request's response that sets no policy itself.

    Covers redirects and other ``Response`` objects a private endpoint returns
    directly, which FastAPI does not merge with the dependency's headers. An
    explicit policy (the public, immutable CLA text) is left as declared.
    Mount it innermost so the public cache sees the final header.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_no_store(message: Message) -> None:
            # The route marks scope["state"] after this middleware is entered.
            if message["type"] == "http.response.start" and scope.get("state", {}).get(
                "no_store"
            ):
                headers = MutableHeaders(raw=list(message.get("headers", [])))
                if "cache-control" not in headers:
                    headers["Cache-Control"] = NO_STORE
                    message["headers"] = headers.raw
            await send(message)

        await self.app(scope, receive, send_no_store)
