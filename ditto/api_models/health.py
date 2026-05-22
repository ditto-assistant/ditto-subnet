"""Wire shape for the ``GET /health`` endpoint."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class HealthResponse(BaseModel):
    """Status snapshot returned by :func:`ditto.api_server.endpoints.health`.

    Endpoint returns HTTP 200 when ``status == "ok"`` and HTTP 503 when
    any dependency is ``"down"``. Per-dependency fields let callers
    distinguish a wedged Postgres from a flaky chain reader without
    parsing log lines.

    The ``commit`` field carries the git revision the running process
    was built from (or ``"unknown"`` when no git checkout is available,
    e.g. inside a Docker image without ``.git``). Mirrors the MVP spec
    contract that ``/health`` returns liveness *and* commit hash.
    """

    status: Literal["ok", "down"]
    """Overall health: ``"ok"`` only when every dependency is up."""

    db: Literal["ok", "down"]
    """Postgres reachability, probed by ``SELECT 1`` per request."""

    chain: Literal["ok", "down"]
    """Pylon reachability, probed by ``ChainClient.get_latest_block``."""

    commit: str
    """Git commit hash of the running build. ``"unknown"`` outside a checkout."""
