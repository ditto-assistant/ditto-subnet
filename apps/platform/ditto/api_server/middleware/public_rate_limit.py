"""Per-client-IP request limit for the unauthenticated expensive routes.

Defense in depth behind the edge proxy (ditto-subnet#447). Caddy has no rate
limit and Cloudflare is DNS-only, so without this nothing stops one client from
driving upload verification (chain RPC, payment proofs, object storage,
fingerprinting) or uncached public reads straight into Postgres.

Off unless ``DITTO_PUBLIC_RATE_LIMIT_PER_MINUTE`` is positive; the factory does
not even install the middleware at ``0``. Only these routers are limited, and
none of their routes authenticate a validator, screener, operator, or miner
session, so no authenticated caller can be turned away here:

- ``/api/v1/upload/*``: pricing, the pre-payment check, and the full upload.
  Rejection happens before the multipart body is read.
- ``/api/v1/retrieval/*``: the miner CLI's status polls, each a DB read.
- ``/api/v1/public/*``: the dashboard read API. This middleware sits inside
  :class:`PublicCacheMiddleware`, so cache hits never count; what is limited is
  exactly the traffic that reaches the database (misses, cache-busting query
  strings, and ``no-store`` routes).

The client is :func:`ditto.api_server.artifact_audit.client_ip`, i.e. the ASGI
peer. Behind the proxy that is already the proxy-appended hop: uvicorn's
proxy-headers support rewrites the peer from ``X-Forwarded-For`` only when the
connection comes from ``FORWARDED_ALLOW_IPS`` (default ``127.0.0.1``, where
Caddy connects from), taking the right-most untrusted entry. A forwarding
header from any other peer is ignored, so a caller cannot choose its bucket.

Buckets are per process and in memory. IPv6 is keyed by /64, since one client
can rotate freely through its prefix.
"""

from __future__ import annotations

import math
import time
from collections import OrderedDict
from collections.abc import Callable
from ipaddress import IPv6Address, ip_address, ip_network
from typing import TYPE_CHECKING

from starlette.middleware.base import BaseHTTPMiddleware

from ditto.api_server.artifact_audit import client_ip
from ditto.api_server.middleware.error_envelope import (
    ERROR_CODE_RATE_LIMITED,
    envelope_response,
)
from ditto.metrics import PUBLIC_RATE_LIMITED

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response

# Route prefix -> the closed ``route`` label on the refusal counter.
_LIMITED_PREFIXES = {
    "/api/v1/upload/": "upload",
    "/api/v1/retrieval/": "retrieval",
    "/api/v1/public/": "public",
}

# Backstop on distinct clients held at once. Idle buckets are already dropped
# once full, so this only binds under a flood of distinct addresses; the oldest
# bucket goes first, which at worst hands that client a fresh bucket.
_MAX_CLIENTS = 65_536


def rate_limit_key(address: str | None) -> str:
    """Bucket key for a client address: the address, or its /64 for IPv6."""
    try:
        parsed = ip_address(address or "")
    except ValueError:
        return address or "-"
    if isinstance(parsed, IPv6Address):
        if parsed.ipv4_mapped is not None:
            return str(parsed.ipv4_mapped)
        return str(ip_network(f"{parsed}/64", strict=False))
    return str(parsed)


class TokenBuckets:
    """Per-key token buckets holding one minute of burst, refilled continuously.

    Buckets are kept in least-recently-used order. A bucket untouched for a full
    minute has refilled completely, which is indistinguishable from having no
    bucket, so it is dropped from the front on the next call. Memory therefore
    tracks the clients seen in the last minute, capped at ``max_clients``.
    """

    def __init__(
        self,
        per_minute: int,
        *,
        now: Callable[[], float] = time.monotonic,
        max_clients: int = _MAX_CLIENTS,
    ) -> None:
        self._capacity = float(per_minute)
        self._rate = per_minute / 60.0
        self._now = now
        self._max_clients = max_clients
        self._buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()

    def __len__(self) -> int:
        return len(self._buckets)

    def acquire(self, key: str) -> float:
        """Spend one token for ``key``; return ``0`` if admitted, else the wait."""
        now = self._now()
        while self._buckets:
            oldest, (_, touched) = next(iter(self._buckets.items()))
            if now - touched < 60.0 and len(self._buckets) < self._max_clients:
                break
            del self._buckets[oldest]
        tokens, touched = self._buckets.pop(key, (self._capacity, now))
        tokens = min(self._capacity, tokens + (now - touched) * self._rate)
        if tokens < 1.0:
            self._buckets[key] = (tokens, now)
            return (1.0 - tokens) / self._rate
        self._buckets[key] = (tokens - 1.0, now)
        return 0.0


class PublicRateLimitMiddleware(BaseHTTPMiddleware):
    """Answer ``429`` + ``Retry-After`` once a client exhausts its bucket."""

    def __init__(
        self,
        app,
        *,
        per_minute: int,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(app)
        self._buckets = TokenBuckets(per_minute, now=now)

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path
        route = next(
            (
                label
                for prefix, label in _LIMITED_PREFIXES.items()
                if path.startswith(prefix)
            ),
            None,
        )
        if route is None:
            return await call_next(request)
        wait = self._buckets.acquire(rate_limit_key(client_ip(request)))
        if wait == 0.0:
            return await call_next(request)
        PUBLIC_RATE_LIMITED.labels(route=route).inc()
        return envelope_response(
            429,
            ERROR_CODE_RATE_LIMITED,
            "too many requests from this client; retry later",
            headers={"Retry-After": str(max(1, math.ceil(wait)))},
        )
