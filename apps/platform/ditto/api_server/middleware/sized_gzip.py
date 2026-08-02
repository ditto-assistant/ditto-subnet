"""GZip middleware whose size floor survives BaseHTTPMiddleware re-chunking.

Starlette's ``GZipMiddleware`` skips compression only when the *first* body
message is complete (``more_body`` false) and shorter than ``minimum_size``.
Any ``BaseHTTPMiddleware`` sitting between it and the endpoint (here:
``PublicCacheMiddleware``) re-chunks every response so the first body message
arrives with ``more_body=True``, which routes even a tiny ``/health`` body down
the unconditional streaming-compression path. This subclass restores the floor
by trusting the declared ``Content-Length`` header, which BaseHTTPMiddleware
preserves: when it is present and under ``minimum_size`` the response is
forwarded verbatim. Responses without a ``Content-Length`` (true streaming)
keep stock behavior.
"""

from __future__ import annotations

from starlette.datastructures import Headers
from starlette.middleware.gzip import GZipMiddleware, GZipResponder
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class _SizedGZipResponder(GZipResponder):
    """``GZipResponder`` that honors ``minimum_size`` via ``Content-Length``."""

    def __init__(self, app: ASGIApp, minimum_size: int, compresslevel: int) -> None:
        super().__init__(app, minimum_size, compresslevel=compresslevel)
        self._below_floor = False

    async def send_with_compression(self, message: Message) -> None:
        if message["type"] == "http.response.start":
            length = Headers(raw=message["headers"]).get("content-length", "")
            self._below_floor = length.isdigit() and int(length) < self.minimum_size
        elif self._below_floor and message["type"] == "http.response.body":
            # Forward the small response untouched (headers included): no
            # compression, no Content-Length rewrite, no Vary append.
            if not self.started:
                self.started = True
                await self.send(self.initial_message)
            await self.send(message)
            return
        await super().send_with_compression(message)


class SizedGZipMiddleware(GZipMiddleware):
    """``GZipMiddleware`` wiring in the Content-Length-aware responder."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and "gzip" in Headers(scope=scope).get(
            "Accept-Encoding", ""
        ):
            responder = _SizedGZipResponder(
                self.app, self.minimum_size, compresslevel=self.compresslevel
            )
            await responder(scope, receive, send)
            return
        await super().__call__(scope, receive, send)
