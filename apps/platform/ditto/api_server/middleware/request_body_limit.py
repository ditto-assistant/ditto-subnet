"""Bound every request body before any endpoint parses or authenticates it.

Endpoints enforce their own size caps, but only after the body has been read:
Starlette spools a multipart upload to disk in full before ``/upload/agent``
sees a byte, and the validator and screener routes call ``request.body()``
ahead of their header-only auth. Nothing upstream bounds the body either
(ditto-subnet#2770), so one unauthenticated request could fill a disk or the
event loop's memory.

This is a pure ASGI middleware, so it sees the raw ``receive`` channel rather
than a re-buffered copy. Each request gets one cap: the first route pattern
that fully matches the path, else the default. A declared ``Content-Length``
over the cap is answered ``413`` before anything reads the body. Otherwise
``receive`` is wrapped to count the bytes actually streamed, which also covers
chunked bodies with no ``Content-Length``; once the running total passes the
cap the read fails, whatever the app was about to answer is dropped, and the
client gets the same ``413`` envelope.

The caps bound the transport, not the endpoint contract. A route cap is the
endpoint's own limit (plus multipart framing where a file part is checked), so
the endpoint's check stays in place behind it; the factory builds that table.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ditto.api_server.middleware.error_envelope import (
    ERROR_CODE_REQUEST_TOO_LARGE,
    envelope_response,
)

logger = logging.getLogger(__name__)


class RequestBodyTooLarge(Exception):
    """Raised from ``receive`` once the streamed body passes its cap.

    Deliberately not a ``ValueError``: body parsers that swallow decode errors
    (``request.json()`` callers) must not mistake it for a malformed body.
    """


class RequestBodyLimitMiddleware:
    """Answer ``413`` once a request body exceeds its route's byte cap."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        default_max_bytes: int,
        route_max_bytes: Iterable[tuple[str, int]] = (),
    ) -> None:
        self.app = app
        self._default_max_bytes = default_max_bytes
        self._route_max_bytes = [
            (re.compile(pattern), limit) for pattern, limit in route_max_bytes
        ]

    def max_bytes_for(self, path: str) -> int:
        """The cap for ``path``: the first matching route pattern, else default."""
        for pattern, limit in self._route_max_bytes:
            if pattern.fullmatch(path):
                return limit
        return self._default_max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = self.max_bytes_for(scope["path"])
        declared = Headers(scope=scope).get("content-length", "")
        if declared.isdigit() and int(declared) > limit:
            await self._reject(scope, receive, send, limit)
            return

        received = 0
        exceeded = False
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received, exceeded
            if exceeded:
                raise RequestBodyTooLarge
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    exceeded = True
                    raise RequestBodyTooLarge
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal response_started
            # After an overflow the app can only be answering for the aborted
            # read (FastAPI turns it into a 400); the 413 below replaces it.
            if exceeded:
                return
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except Exception:
            if not exceeded:
                raise
        if exceeded:
            if response_started:
                logger.warning(
                    f"request body over {limit} bytes on {scope['path']} "
                    "after the response started; connection dropped"
                )
                return
            await self._reject(scope, receive, send, limit)

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send, limit: int) -> None:
        logger.warning(f"request body over {limit} bytes on {scope['path']} refused")
        response = envelope_response(
            413,
            ERROR_CODE_REQUEST_TOO_LARGE,
            f"request body exceeds {limit} bytes",
            headers={"Connection": "close"},
        )
        await response(scope, receive, send)
