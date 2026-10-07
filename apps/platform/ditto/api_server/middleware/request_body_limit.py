"""Cap an HTTP body before the application can spool it.

Starlette writes a multipart file part to a spooled temporary file as the
bytes arrive, and the upload signature is checked only after that parse. A
declared ``Content-Length`` above the cap is refused without calling the
application. A chunked body is cut off at the same cap, so the spool cannot
grow past the largest body the API actually accepts.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

# A published transcript is the largest body a route accepts. The default
# upload tarball is 20 MiB and fits inside this. One extra MiB covers
# multipart framing when an operator raises the tarball cap above 32 MiB.
_TRANSCRIPT_MAX_BYTES = 32 << 20
_MULTIPART_FRAMING_BYTES = 1 << 20

_TOO_LARGE = b'{"detail":"request body is too large"}'

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


def request_body_limit_bytes(tarball_max_bytes: int) -> int:
    """Bytes the edge and the application may accept for one request."""
    if type(tarball_max_bytes) is not int or tarball_max_bytes < 1:
        raise ValueError("tarball_max_bytes must be a positive int")
    return max(
        _TRANSCRIPT_MAX_BYTES, tarball_max_bytes + _MULTIPART_FRAMING_BYTES
    )


class RequestBodyLimitMiddleware:
    """Refuse a body larger than ``max_bytes`` before it is parsed."""

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        if type(max_bytes) is not int or max_bytes < 1:
            raise ValueError("max_bytes must be a positive int")
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        claimed = _content_length(scope)
        if claimed is not None and claimed > self.max_bytes:
            await _send_too_large(send)
            return

        received = 0
        rejected = False
        started = False

        async def guarded_send(message: Message) -> None:
            nonlocal started
            if rejected:
                return
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        async def limited_receive() -> Message:
            nonlocal received, rejected, started
            if rejected:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] != "http.request":
                return message
            chunk = message.get("body", b"") or b""
            if received + len(chunk) > self.max_bytes:
                rejected = True
                if not started:
                    await _send_too_large(send)
                    started = True
                return {"type": "http.disconnect"}
            received += len(chunk)
            return message

        try:
            await self.app(scope, limited_receive, guarded_send)
        except Exception:
            if not rejected:
                raise


def _content_length(scope: Scope) -> int | None:
    for name, value in scope.get("headers", ()):
        if name.lower() == b"content-length":
            try:
                claimed = int(value)
            except (TypeError, ValueError):
                return None
            if claimed < 0:
                return None
            return claimed
    return None


async def _send_too_large(send: Send) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(_TOO_LARGE)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": _TOO_LARGE})
