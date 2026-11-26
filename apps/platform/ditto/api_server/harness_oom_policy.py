"""When a proven harness OOM starts counting against the agent (SN-488).

The scorer proves an OOM at the harness's own memory cap and the validator
hands it back as ``sandbox_oom`` / ``harness_oom``. Charging that as a failed
agent run (attempt spent, agent cooldown, agent-attributable for terminal
withdrawal and the public failure code) takes effect only for leases issued at
or after :data:`HARNESS_OOM_CHARGE_EFFECTIVE_AT`. The memory cap dropped from
8192 MiB to 2048 MiB on 2026-11-22, and miners get notice before an OOM at the
new cap costs them anything.

Before then the hand-back is parked exactly like other no-fault
infrastructure, under its own detail :data:`HARNESS_OOM_GRACE_FAILURE_DETAIL`.
The distinct detail matters: ``harness_oom`` is in the agent-attributable sets
that terminal withdrawal, the stuck list and the public failure code read, so
reusing it would still blame the agent through those paths.

Keyed on the lease's issue time, not the report time, so a run already under
way at the effective time is never charged.
"""

from __future__ import annotations

from datetime import UTC, datetime

HARNESS_OOM_CHARGE_EFFECTIVE_AT = datetime(2026, 11, 25, 17, 0, tzinfo=UTC)
"""Wed 2026-11-25 12:00 America/Toronto (EST, UTC-5)."""

HARNESS_OOM_GRACE_FAILURE_DETAIL = "harness_oom_grace"
"""Ticket detail for a proven harness OOM on a lease issued before the effective
time: recorded for diagnosis, deliberately not agent-attributable."""


def harness_oom_charged(lease_issued_at: datetime) -> bool:
    """Whether a proven harness OOM on this lease counts against the agent.

    Reads :data:`HARNESS_OOM_CHARGE_EFFECTIVE_AT` at call time so tests can
    pin either side of the boundary without depending on the wall clock.
    """
    issued = (
        lease_issued_at
        if lease_issued_at.tzinfo is not None
        else lease_issued_at.replace(tzinfo=UTC)
    )
    return issued >= HARNESS_OOM_CHARGE_EFFECTIVE_AT


__all__ = [
    "HARNESS_OOM_CHARGE_EFFECTIVE_AT",
    "HARNESS_OOM_GRACE_FAILURE_DETAIL",
    "harness_oom_charged",
]
