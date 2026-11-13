"""Shadow ledger rules: one closed window, one node, one row.

``verified_units`` is held GiB-hours times availability, and only when the
window is eligible. It is the quantity a priced, capped Edge track share would
divide among nodes. In shadow, nothing pays: every row Platform stores is
``weight_eligible = false``, and ``hypothetical_share`` only shows what a node
*would* receive.

Rules that keep it hard to game:

* Pay grows with verified bytes held, never with identities or claimed traffic.
* One wrong proof (an integrity failure) or one answer for a revoked object (a
  violation) makes the whole window ineligible.
* Re-downloading to answer counts against availability like a miss.
* Every number is rounded to 9 decimal places, so independent folds of the
  same inputs agree byte for byte.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from ditto_edge_protocol.challenges import Outcome, Purpose
from ditto_edge_protocol.limits import GIB

_PRECISION = 9
_COUNTED = frozenset({"passed", "failed", "missed", "refilled", "refused", "pending"})


@dataclass(frozen=True)
class Observation:
    purpose: Purpose
    outcome: Outcome


@dataclass(frozen=True)
class HoldInterval:
    """When this node held one acknowledged, unrevoked object."""

    size_bytes: int
    start: float
    end: float | None
    """``None`` while still held."""


@dataclass(frozen=True)
class LedgerParams:
    min_challenges: int
    availability_floor: float


@dataclass(frozen=True)
class WindowScore:
    scheduled: int
    """Possession and retrieval slots that count toward availability."""
    passed: int
    failed: int
    missed: int
    refilled: int
    refused: int
    revocation_checks: int
    revocation_violations: int
    held_gib_hours: float
    availability: float
    eligible: bool
    verified_units: float
    reasons: tuple[str, ...]
    """Why an ineligible window is ineligible, in a fixed order."""


def score_window(
    *,
    observations: Sequence[Observation],
    holds: Sequence[HoldInterval],
    window_start: float,
    window_end: float,
    params: LedgerParams,
) -> WindowScore:
    """Score one closed window for one node.

    Call it only after the window and every answer deadline in it have
    passed. A slot still ``pending`` then never got an answer and counts as
    missed.
    """
    passed = failed = missed = refilled = refused = 0
    revocation_checks = violations = 0
    for observation in observations:
        if observation.purpose == "revoked":
            if observation.outcome in ("refused_ok", "violation"):
                revocation_checks += 1
                if observation.outcome == "violation":
                    violations += 1
            elif observation.outcome in ("missed", "pending"):
                missed += 1
            continue
        if observation.outcome not in _COUNTED:
            continue
        if observation.outcome == "passed":
            passed += 1
        elif observation.outcome == "failed":
            failed += 1
        elif observation.outcome == "refilled":
            refilled += 1
        elif observation.outcome == "refused":
            refused += 1
        else:
            missed += 1
    scheduled = passed + failed + missed + refilled + refused
    availability = round(passed / scheduled, _PRECISION) if scheduled else 0.0
    held = round(_held_gib_hours(holds, window_start, window_end), _PRECISION)

    reasons: list[str] = []
    if scheduled < params.min_challenges:
        reasons.append("too_few_challenges")
    if failed:
        reasons.append("integrity_failure")
    if violations:
        reasons.append("revocation_violation")
    if scheduled and availability < params.availability_floor:
        reasons.append("availability_below_floor")
    if held <= 0:
        reasons.append("nothing_held")
    eligible = not reasons
    units = round(held * availability, _PRECISION) if eligible else 0.0
    return WindowScore(
        scheduled=scheduled,
        passed=passed,
        failed=failed,
        missed=missed,
        refilled=refilled,
        refused=refused,
        revocation_checks=revocation_checks,
        revocation_violations=violations,
        held_gib_hours=held,
        availability=availability,
        eligible=eligible,
        verified_units=units,
        reasons=tuple(reasons),
    )


def _held_gib_hours(
    holds: Sequence[HoldInterval], window_start: float, window_end: float
) -> float:
    total = 0.0
    for hold in holds:
        start = max(hold.start, window_start)
        end = window_end if hold.end is None else min(hold.end, window_end)
        if end > start:
            total += hold.size_bytes * (end - start)
    return total / 3600.0 / GIB


def hypothetical_shares(units: Mapping[str, float]) -> dict[str, float]:
    """Each node's share of the window's verified units, summing to 1 or 0.

    A shadow read-out only: what a node would receive of an Edge track share.
    """
    total = sum(value for value in units.values() if value > 0)
    if total <= 0:
        return dict.fromkeys(units, 0.0)
    return {
        key: round(value / total, _PRECISION) if value > 0 else 0.0
        for key, value in units.items()
    }
