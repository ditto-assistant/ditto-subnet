"""Single-flight TTL cache for world-readable public GET responses.

The dashboard and third-party pollers hit ``/api/v1/public/*`` hundreds of
thousands of times a day. Every one of those responses is identical for every
viewer, and each endpoint already declares its freshness contract via
``Cache-Control: public, max-age=N`` — but nothing in front of uvicorn
actually caches (Caddy has no cache module and there is no CDN), so every
poll ran its queries against Postgres.

This middleware enforces the endpoints' own declared TTLs in-process:

- Only ``GET`` requests under ``/api/v1/public/`` and the generator
  competition's public board and rounds are considered.
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
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING

from starlette.datastructures import Headers
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
from starlette.types import Message, Scope

if TYPE_CHECKING:
    from starlette.requests import Request

_PREFIX = "/api/v1/public/"
# World-readable reads outside ``/public/``: the generator board and rounds
# serve every viewer the same body and declare ``max-age``, but without the
# cache each dashboard poll recomputed the ledger against Postgres.
_EXTRA_PREFIXES = (
    "/api/v1/generator/board",
    "/api/v1/generator/rounds",
    "/api/v1/generator/submissions",
)


def is_cacheable_path(path: str) -> bool:
    """Whether a GET on ``path`` may be served from the shared cache."""
    return path.startswith(_PREFIX) or any(
        path == prefix or path.startswith(prefix + "/") for prefix in _EXTRA_PREFIXES
    )


_MAX_ENTRIES = 512
_REFRESH_FAILURE_BACKOFF_SECONDS = 5.0

_CACHEABLE_HEADERS = (
    "cache-control",
    "content-encoding",
    "content-type",
    "vary",
    "x-content-type-options",
)

logger = logging.getLogger(__name__)


def _declared_etag(headers: Mapping[str, str]) -> str | None:
    """The endpoint's own validator, as it applies to this representation.

    Content-addressed endpoints (pinned CLA text, avatars) publish a digest
    ETag that clients pin in If-None-Match; replacing it would turn every
    revalidation into a full 200. A declared strong ETag names the identity
    bytes, so an encoded representation carries it in weak form: distinct
    bytes never share a strong validator, and weak revalidation still answers
    a pinned digest with a 304. Every path that serves a declared ETag
    through this cache, stored or passed through, uses this one rule.
    """
    declared = headers.get("etag")
    if declared and headers.get("content-encoding") and not declared.startswith("W/"):
        return "W/" + declared
    return declared


def _entry_etag(headers: Mapping[str, str], body: bytes) -> str:
    """Keep an endpoint's own validator; otherwise derive one from the body."""
    return _declared_etag(headers) or compute_etag(body)


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
    validators (``W/"..."``). If-None-Match uses weak comparison (RFC 9110
    13.1.2), so the prefix is ignored on both sides: an endpoint may declare a
    weak ETag of its own.
    """
    if not header:
        return False
    current = etag.removeprefix("W/")
    for raw in header.split(","):
        token = raw.strip()
        if token == "*":
            return True
        if token.removeprefix("W/") == current:
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


def _cache_control(headers: Headers) -> str:
    """Every Cache-Control field, so a later ``no-store`` is never missed."""
    return ", ".join(headers.getlist("cache-control"))


def _stored_headers(headers: Headers) -> dict[str, str]:
    """The cacheable headers, keeping every field of a split header."""
    stored = {
        name: ", ".join(headers.getlist(name))
        for name in _CACHEABLE_HEADERS
        if name in headers
    }
    stored.setdefault("vary", "Accept-Encoding")
    return stored


def _policy_from_cache_control(value: str | None) -> tuple[float, float]:
    if not value:
        return 0.0, 0.0
    # Directives by name, so an extension such as x-no-cache-hint=1 or
    # x-max-age=60 is neither a refusal nor a lifetime.
    directives = {
        name.strip(): argument.strip()
        for name, _, argument in (
            directive.partition("=") for directive in value.lower().split(",")
        )
    }
    # no-cache forbids serving without revalidation, which this cache never does.
    if directives.keys() & {"no-store", "private", "no-cache"}:
        return 0.0, 0.0

    def seconds(name: str) -> float:
        argument = directives.get(name, "")
        return float(argument) if argument.isdigit() else 0.0

    return seconds("max-age"), seconds("stale-while-revalidate")


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
        """Run the downstream app and capture the body when cacheable.

        The validator is dropped downstream, as on a background refresh, so the
        endpoint always returns the 200 this cache stores and every 304 for a
        cacheable entry is built here with the entry's own ETag and Vary.
        """
        inm = request.headers.get("if-none-match")
        request.scope["headers"] = [
            (name, value)
            for name, value in request.scope["headers"]
            if name.lower() != b"if-none-match"
        ]
        response = await call_next(request)
        ttl, stale_ttl = _policy_from_cache_control(_cache_control(response.headers))
        if response.status_code != 200 or ttl <= 0:
            etag = _declared_etag(response.headers)
            if response.status_code == 200 and etag:
                response.headers["etag"] = etag
            if response.status_code == 200 and etag and if_none_match(inm, etag):
                # Not cacheable, but the client's validator still matches.
                async for _chunk in response.body_iterator:
                    pass
                return Response(
                    status_code=304,
                    headers={
                        name: value
                        for name, value in _stored_headers(response.headers).items()
                        if name in ("cache-control", "vary")
                    }
                    | {"ETag": etag},
                ), None
            return response, None
        body = b"".join([chunk async for chunk in response.body_iterator])
        headers = _stored_headers(response.headers)
        etag = _entry_etag(response.headers, body)
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
            # This internal refresh has no client to disconnect. Advertising
            # one here cancels StreamingResponse after its first chunk, then
            # caches a truncated body as a successful 200 response.
            await asyncio.Future()
            raise AssertionError("unreachable")

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
            ttl, stale_ttl = _policy_from_cache_control(_cache_control(headers_obj))
            if status != 200 or ttl <= 0:
                self._backoff_stale(key)
                logger.warning(
                    "public cache background refresh returned status=%d key=%s",
                    status,
                    key,
                )
                return
            body = b"".join(body_parts)
            headers = _stored_headers(headers_obj)
            expires_at = self._now() + ttl
            self._store(
                key,
                _Entry(
                    status,
                    headers,
                    body,
                    _entry_etag(headers_obj, body),
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
            or not is_cacheable_path(request.url.path)
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
