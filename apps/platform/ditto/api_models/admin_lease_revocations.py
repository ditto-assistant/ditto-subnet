"""Admin: read why the platform ended a validator's lease before its deadline.

``validator_lease_audit`` has recorded the reason and the evidence behind every
platform-initiated revocation since it shipped, and until now nothing in
``api_server`` or ``api_models`` could read it. That gap is most of why a lease
incident took a day to diagnose: the one table built to answer "why was this run
killed" was reachable only by opening a psql session against production.

``evidence`` is returned whole and deliberately untyped. ``reason`` alone is a
bare code like ``idle_capacity_reports_slot_free``; the evidence carries the
heartbeat sample, the lease age, the original deadline, the attempt count and
the capacity snapshot the verdict was actually taken on, which is what turns the
answer from archaeology into a read. Its keys vary per reason code by
construction (``lease_liveness`` builds them per verdict), so modelling it as a
closed schema would drop fields exactly when an unusual revocation made them
interesting.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel


class AdminLeaseRevocation(BaseModel):
    """One platform-initiated end of a validator lease, with its evidence."""

    audit_id: UUID
    agent_id: UUID
    validator_hotkey: str
    slot_id: str
    bench_version: int
    action: str
    """``force_expired`` (issuance lanes) or ``closed_unserviceable`` (retest).

    Plain ``str`` rather than a ``Literal``: a new lane must never turn an
    operator's read into a 500 at the moment they most need it.
    """
    reason: str
    """The liveness verdict that admitted the revocation."""
    context: str
    """Which lane acted: ``issue_ticket``, ``issue_rollout_ticket``,
    or ``score_retest``."""
    recorded_at: datetime
    evidence: dict[str, Any]


class AdminLeaseRevocationList(BaseModel):
    """A page of revocations, newest first."""

    generated_at: datetime
    total: int
    """Matching rows ignoring ``limit``/``offset``, so a console can paginate."""
    revocations: list[AdminLeaseRevocation]
