"""Wire shape for the ``GET /health`` endpoint."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class HealthResponse(BaseModel):
    """Response shape for ``GET /health``."""

    status: Literal["ok", "down"]
    """Overall health: ``"ok"`` only when every dependency is up."""

    db: Literal["ok", "down"]
    """Postgres reachability, probed by ``SELECT 1`` per request."""

    chain: Literal["ok", "down"]
    """Pylon reachability, probed by ``ChainClient.get_latest_block``."""

    commit: str
    """Git commit hash of the running build. ``"unknown"`` outside a checkout."""
