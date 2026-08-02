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
- The store is bounded (LRU, ``_MAX_ENTRIES``) so per-agent routes cannot
  grow memory without limit.

Set ``PUBLIC_CACHE_DISABLED=1`` to bypass entirely (ops kill switch).
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import TYPE_CHECKING

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

if TYPE_CHECKING:
    from starlette.requests import Request

_PREFIX = "/api/v1/public/"
_MAX_ENTRIES = 512
_MAX_AGE = re.compile(r"max-age=(\d+)")

_CACHEABLE_HEADERS = ("cache-control", "content-type")


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
    __slots__ = ("body", "etag", "expires_at", "headers", "status")

    def __init__(
        self,
        status: int,
        headers: dict[str, str],
        body: bytes,
        etag: str,
        expires_at: float,
    ) -> None:
        self.status = status
        self.headers = headers
        self.body = body
        self.etag = etag
        self.expires_at = expires_at


def _ttl_from_cache_control(value: str | None) -> float:
    if not value:
        return 0.0
    lowered = value.lower()
    if "no-store" in lowered or "private" in lowered:
        return 0.0
    match = _MAX_AGE.search(lowered)
    return float(match.group(1)) if match else 0.0


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
        # The 200 this revalidates carries "Vary: Accept-Encoding" (added by
        # the outer gzip layer, which never touches an under-floor 304); RFC
        # 9110 §15.4.5 says the 304 must repeat it.
        headers = {
            "ETag": entry.etag,
            "X-Public-Cache": cache_state,
            "Vary": "Accept-Encoding",
        }
        cache_control = entry.headers.get("cache-control")
        if cache_control is not None:
            headers["Cache-Control"] = cache_control
        return Response(status_code=304, headers=headers)

    @classmethod
    def _respond(cls, entry: _Entry, inm: str | None, cache_state: str) -> Response:
        """Serve a cached entry: a 304 when the client's ETag matches, else 200."""
        if if_none_match(inm, entry.etag):
            return cls._not_modified(entry, cache_state)
        headers = dict(entry.headers)
        headers["ETag"] = entry.etag
        headers["X-Public-Cache"] = cache_state
        return Response(content=entry.body, status_code=entry.status, headers=headers)

    async def _fetch(
        self, request: Request, call_next
    ) -> tuple[Response, _Entry | None]:
        """Run the downstream app and capture the body when cacheable."""
        response = await call_next(request)
        ttl = _ttl_from_cache_control(response.headers.get("cache-control"))
        if response.status_code != 200 or ttl <= 0:
            return response, None
        body = b"".join([chunk async for chunk in response.body_iterator])
        headers = {
            name: value
            for name, value in response.headers.items()
            if name.lower() in _CACHEABLE_HEADERS
        }
        etag = compute_etag(body)
        entry = _Entry(response.status_code, headers, body, etag, self._now() + ttl)
        rebuilt = Response(
            content=body, status_code=response.status_code, headers=dict(headers)
        )
        rebuilt.headers["ETag"] = etag
        rebuilt.headers["X-Public-Cache"] = "MISS"
        return rebuilt, entry

    async def dispatch(self, request: Request, call_next) -> Response:
        if (
            self._disabled
            or request.method != "GET"
            or not request.url.path.startswith(_PREFIX)
        ):
            return await call_next(request)

        key = f"{request.url.path}?{request.url.query}"
        inm = request.headers.get("if-none-match")
        cached = self._get_fresh(key)
        if cached is not None:
            return self._respond(cached, inm, "HIT")

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
