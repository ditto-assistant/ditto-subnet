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
    """Git commit hash of the running build. ``"unknown"`` outside a checkout.

    Resolved once, at process start. This is what is actually in service --
    not what happens to be on disk.
    """

    checked_out_commit: str
    """Git commit hash currently checked out in the deploy directory.

    Re-read (cached briefly) on each probe, so it tracks the filesystem rather
    than the process. ``"unknown"`` outside a checkout.
    """

    commit_drift: bool
    """``True`` when the process is provably not running the checked-out code.

    A deploy that checks out a new revision and then fails before restarting
    the app leaves exactly this state, and nothing at the git layer reveals it
    -- ``git rev-parse HEAD`` reports the new revision either way. Only
    ``True`` when both commits are known; an unresolvable revision is an
    absence of evidence, not drift.

    Deliberately does NOT affect ``status`` or the HTTP code: drift means a
    deploy needs attention, not that this instance should be pulled out of
    service. Turning it into a 503 would convert a stale-code incident into an
    outage.
    """
