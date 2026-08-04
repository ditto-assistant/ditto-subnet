"""Single-flight TTL cache for world-readable public GET responses.

The dashboard and third-party pollers hit ``/api/v1/public/*`` hundreds of
thousands of times a day. Every one of those responses is identical for every
viewer, and each endpoint already declares its freshness contract via
``Cache-Control: public, max-age=N`` — but nothing in front of uvicorn
actually caches (Caddy has no cache module and there is no CDN), so every
poll ran its queries against Postgres.

This middleware enforces the endpoints' own declared TTLs in-process:

- Only ``GET`` requests under ``/api/v1/public/`` are considered.
- A response is cached only when it is a 200 whose ``Cache-Control`` carries
  a positive ``max-age``; ``no-store``/``private`` responses pass through.
- Concurrent misses on one key are single-flighted: one request computes,
  the rest await and share the same body, so a poll storm at TTL expiry
  costs one database pass instead of dozens.
- ``stale-while-revalidate`` responses return their last snapshot immediately
  while one detached refresh rebuilds the entry through the inner app.
- Identity and gzip representations are keyed separately, so cache hits reuse
  already-compressed bytes instead of burning CPU recompressing large payloads.
- The store is bounded (LRU, ``_MAX_ENTRIES``) so per-agent routes cannot
  grow memory without limit.

Set ``PUBLIC_CACHE_DISABLED=1`` to bypass entirely (ops kill switch).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import TYPE_CHECKING

from starlette.datastructures import Headers
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
from starlette.types import Message, Scope

if TYPE_CHECKING:
    from starlette.requests import Request

_PREFIX = "/api/v1/public/"
_MAX_ENTRIES = 512
_MAX_AGE = re.compile(r"max-age=(\d+)")
_STALE_WHILE_REVALIDATE = re.compile(r"stale-while-revalidate=(\d+)")
_REFRESH_FAILURE_BACKOFF_SECONDS = 5.0

_CACHEABLE_HEADERS = ("cache-control", "content-encoding", "content-type", "vary")

logger = logging.getLogger(__name__)


def compute_etag(body: bytes) -> str:
    """Return a strong ETag (quoted) for ``body``.

    A truncated SHA-256 is plenty for revalidation: collisions across two
    distinct public responses at the same URL are astronomically unlikely, and
    the shorter token keeps the header small. Computed once when the entry is
    stored (or, for the dashboard HTML, once at boot).
    """
    return '"' + hashlib.sha256(body).hexdigest()[:32] + '"'


def if_none_match(header: str | None, etag: str) -> bool:
    """Return ``True`` when ``If-None-Match`` matches ``etag`` (so serve 304).

    Accepts the comma-separated list form, the ``*`` wildcard, and weak
    validators (``W/"..."``); we compare ignoring the weak prefix since our
    strong ETag is byte-stable.
    """
    if not header:
        return False
    for raw in header.split(","):
        token = raw.strip()
        if token == "*":
            return True
        if token.startswith("W/"):
            token = token[2:]
        if token == etag:
            return True
    return False


class _Entry:
    __slots__ = ("body", "etag", "expires_at", "headers", "stale_until", "status")

    def __init__(
        self,
        status: int,
        headers: dict[str, str],
        body: bytes,
        etag: str,
        expires_at: float,
        stale_until: float,
    ) -> None:
        self.status = status
        self.headers = headers
        self.body = body
        self.etag = etag
        self.expires_at = expires_at
        self.stale_until = stale_until


def _policy_from_cache_control(value: str | None) -> tuple[float, float]:
    if not value:
        return 0.0, 0.0
    lowered = value.lower()
    if "no-store" in lowered or "private" in lowered:
        return 0.0, 0.0
    max_age = _MAX_AGE.search(lowered)
    stale = _STALE_WHILE_REVALIDATE.search(lowered)
    return (
        float(max_age.group(1)) if max_age else 0.0,
        float(stale.group(1)) if stale else 0.0,
    )


class PublicCacheMiddleware(BaseHTTPMiddleware):
    """Serve public GET responses from a bounded in-process TTL cache."""

    def __init__(
        self,
        app,
        *,
        now: Callable[[], float] = time.monotonic,
        disabled: bool | None = None,
    ) -> None:
        super().__init__(app)
        self._now = now
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._inflight: dict[str, asyncio.Future[_Entry | None]] = {}
        self._refreshing: dict[str, asyncio.Task[None]] = {}
        self._disabled = (
            os.environ.get("PUBLIC_CACHE_DISABLED", "") == "1"
            if disabled is None
            else disabled
        )

    def _get_fresh(self, key: str) -> _Entry | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry.expires_at <= self._now():
            return None
        self._entries.move_to_end(key)
        return entry

    def _get_stale(self, key: str) -> _Entry | None:
        entry = self._entries.get(key)
        if entry is None or entry.expires_at > self._now():
            return None
        if entry.stale_until <= self._now():
            self._entries.pop(key, None)
            return None
        self._entries.move_to_end(key)
        return entry

    def _store(self, key: str, entry: _Entry) -> None:
        self._entries[key] = entry
        self._entries.move_to_end(key)
        while len(self._entries) > _MAX_ENTRIES:
            self._entries.popitem(last=False)

    @staticmethod
    def _not_modified(entry: _Entry, cache_state: str) -> Response:
        """Build a 304 with an empty body but the entry's revalidation headers."""
        # The cached 200 varies by the identity/gzip representation; RFC 9110
        # §15.4.5 says the 304 must repeat that Vary header.
        headers = {
            "ETag": entry.etag,
            "X-Public-Cache": cache_state,
        }
        cache_control = entry.headers.get("cache-control")
        if cache_control is not None:
            headers["Cache-Control"] = cache_control
        vary = entry.headers.get("vary")
        headers["Vary"] = vary or "Accept-Encoding"
        return Response(status_code=304, headers=headers)

    @classmethod
    def _respond(cls, entry: _Entry, inm: str | None, cache_state: str) -> Response:
        """Serve a cached entry: a 304 when the client's ETag matches, else 200."""
        if if_none_match(inm, entry.etag):
            return cls._not_modified(entry, cache_state)
        headers = dict(entry.headers)
        headers.setdefault("vary", "Accept-Encoding")
        headers["etag"] = entry.etag
        headers["x-public-cache"] = cache_state
        return Response(content=entry.body, status_code=entry.status, headers=headers)

    async def _fetch(
        self, request: Request, call_next
    ) -> tuple[Response, _Entry | None]:
        """Run the downstream app and capture the body when cacheable."""
        response = await call_next(request)
        ttl, stale_ttl = _policy_from_cache_control(
            response.headers.get("cache-control")
        )
        if response.status_code != 200 or ttl <= 0:
            return response, None
        body = b"".join([chunk async for chunk in response.body_iterator])
        headers = {
            name: value
            for name, value in response.headers.items()
            if name.lower() in _CACHEABLE_HEADERS
        }
        headers.setdefault("vary", "Accept-Encoding")
        etag = compute_etag(body)
        expires_at = self._now() + ttl
        entry = _Entry(
            response.status_code,
            headers,
            body,
            etag,
            expires_at,
            expires_at + stale_ttl,
        )
        rebuilt = Response(
            content=body, status_code=response.status_code, headers=dict(headers)
        )
        rebuilt.headers["ETag"] = etag
        rebuilt.headers["X-Public-Cache"] = "MISS"
        rebuilt.headers["Vary"] = headers["vary"]
        return rebuilt, entry

    @staticmethod
    def _cache_key(request: Request) -> str:
        # Gzip now sits inside this middleware, so compressed and identity
        # representations are stored independently and never recompressed on a
        # cache hit. Match SizedGZipMiddleware's representation choice.
        encoding = (
            "gzip"
            if "gzip" in request.headers.get("accept-encoding", "")
            else "identity"
        )
        return f"{request.url.path}?{request.url.query}|{encoding}"

    async def _refresh_from_scope(self, key: str, scope: Scope) -> None:
        """Rebuild one stale entry through the inner ASGI app.

        This bypasses this cache middleware itself but still runs gzip and the
        endpoint/dependency stack. The synthetic GET has no body and drops a
        caller's conditional validator so the rebuild always captures a 200.
        """
        started = False
        status = 500
        raw_headers: list[tuple[bytes, bytes]] = []
        body_parts: list[bytes] = []

        inner_scope = dict(scope)
        inner_scope["headers"] = [
            (name, value)
            for name, value in scope.get("headers", [])
            if name.lower() != b"if-none-match"
        ]

        async def receive() -> Message:
            nonlocal started
            if not started:
                started = True
                return {"type": "http.request", "body": b"", "more_body": False}
            return {"type": "http.disconnect"}

        async def send(message: Message) -> None:
            nonlocal status, raw_headers
            if message["type"] == "http.response.start":
                status = int(message["status"])
                raw_headers = list(message.get("headers", []))
            elif message["type"] == "http.response.body":
                body_parts.append(message.get("body", b""))

        try:
            await self.app(inner_scope, receive, send)
            headers_obj = Headers(raw=raw_headers)
            ttl, stale_ttl = _policy_from_cache_control(
                headers_obj.get("cache-control")
            )
            if status != 200 or ttl <= 0:
                self._backoff_stale(key)
                logger.warning(
                    "public cache background refresh returned status=%d key=%s",
                    status,
                    key,
                )
                return
            body = b"".join(body_parts)
            headers = {
                name: value
                for name, value in headers_obj.items()
                if name.lower() in _CACHEABLE_HEADERS
            }
            headers.setdefault("vary", "Accept-Encoding")
            expires_at = self._now() + ttl
            self._store(
                key,
                _Entry(
                    status,
                    headers,
                    body,
                    compute_etag(body),
                    expires_at,
                    expires_at + stale_ttl,
                ),
            )
        except Exception:
            # Keep serving the bounded stale entry and avoid turning a failed
            # refresh into one retry per incoming poller.
            self._backoff_stale(key)
            logger.warning(
                "public cache background refresh failed key=%s", key, exc_info=True
            )
        finally:
            self._refreshing.pop(key, None)

    def _backoff_stale(self, key: str) -> None:
        entry = self._entries.get(key)
        if entry is not None:
            entry.expires_at = min(
                entry.stale_until,
                self._now() + _REFRESH_FAILURE_BACKOFF_SECONDS,
            )

    def _start_refresh(self, key: str, request: Request) -> None:
        if key in self._refreshing:
            return
        self._refreshing[key] = asyncio.create_task(
            self._refresh_from_scope(key, dict(request.scope)),
            name=f"public-cache-refresh:{request.url.path}",
        )

    async def dispatch(self, request: Request, call_next) -> Response:
        if (
            self._disabled
            or request.method != "GET"
            or not request.url.path.startswith(_PREFIX)
        ):
            return await call_next(request)

        key = self._cache_key(request)
        inm = request.headers.get("if-none-match")
        cached = self._get_fresh(key)
        if cached is not None:
            return self._respond(cached, inm, "HIT")

        stale = self._get_stale(key)
        if stale is not None:
            self._start_refresh(key, request)
            return self._respond(stale, inm, "STALE")

        waiting = self._inflight.get(key)
        if waiting is not None:
            # Another request is already computing this key; share its result.
            try:
                entry = await asyncio.shield(waiting)
            except Exception:
                entry = None
            if entry is not None:
                return self._respond(entry, inm, "HIT")
            return await call_next(request)

        future: asyncio.Future[_Entry | None] = (
            asyncio.get_running_loop().create_future()
        )
        self._inflight[key] = future
        try:
            response, entry = await self._fetch(request, call_next)
            if entry is not None:
                self._store(key, entry)
            if not future.done():
                future.set_result(entry)
            if entry is not None and if_none_match(inm, entry.etag):
                return self._not_modified(entry, "MISS")
            return response
        except BaseException:
            if not future.done():
                future.set_result(None)
            raise
        finally:
            self._inflight.pop(key, None)
