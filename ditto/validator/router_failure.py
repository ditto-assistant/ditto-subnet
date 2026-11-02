"""Fail-closed per-harness failure classification for the router track.

**Telemetry only.** The centralized router scorer already folds any soft-forfeited
harness slice into ``RouterLedgerEntry.combined_score`` (``Σ_h weight_h × (eff_h
if operational and floor else 0)``), and the validator's fold ranks by that one
number. This module never changes a weight; it reads the published ledger's two
per-harness gates — ``operational`` (the router powered the harness end-to-end)
and ``floor_pass`` (the run cleared the frontier-quality correctness floor) — so a
forfeited slice is **named and logged**, not silently zeroed.

Every ambiguity resolves toward "forfeited" (fail-closed), matching the
malformed→fallback discipline of ``resolve_miner_emission_share``: an expected
harness the scorer never reported, or a contract-impossible flag combination,
counts as a lost slice rather than a silent contribution.

Soft weights (CC 0.40 / Codex 0.30 / opencode 0.20 / Grok 0.10) let the report
state the *size* of what a miner forfeited (a Grok failure is 0.10, not the whole
score), while the other slices stay intact.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from ditto.api_models.router_ledger import (
    RouterHarness,
    RouterHarnessResult,
    RouterLedgerEntry,
)


class RouterFailureStage(StrEnum):
    """Why a harness slice did not contribute, from most to least router-fatal.

    Grounded strictly in what the ledger carries: ``ROUTER_BOOT`` and
    ``TELEMETRY`` are inferred at the entry level (nothing operational anywhere, or
    an expected harness absent from the feed); ``HARNESS_RUNTIME`` flags a
    contract-impossible flag pair; ``HARNESS_OPERATIONAL`` and ``CORRECTNESS_FLOOR``
    are the two ordinary per-harness gate failures.
    """

    ROUTER_BOOT = "router_boot"
    HARNESS_RUNTIME = "harness_runtime"
    HARNESS_OPERATIONAL = "harness_operational"
    CORRECTNESS_FLOOR = "correctness_floor"
    TELEMETRY = "telemetry"


# Default soft per-harness weights. The scorer owns the authoritative combine;
# these mirror it so the validator can report the *magnitude* of a forfeited
# slice. Kept here (not config) so the classifier stays a pure function; the
# worker may pass diverging weights explicitly.
DEFAULT_HARNESS_WEIGHTS: Mapping[RouterHarness, float] = {
    RouterHarness.CLAUDE_CODE: 0.40,
    RouterHarness.CODEX: 0.30,
    RouterHarness.OPENCODE: 0.20,
    RouterHarness.GROK: 0.10,
}


@dataclass(frozen=True)
class RouterHarnessOutcome:
    """One harness's classified outcome for one miner router (telemetry)."""

    harness: RouterHarness
    contributed: bool
    stage: RouterFailureStage | None
    weight: float


@dataclass(frozen=True)
class RouterFailureReport:
    """Per-miner classification of every expected harness slice."""

    miner_hotkey: str
    outcomes: tuple[RouterHarnessOutcome, ...]
    dominant_stage: RouterFailureStage | None

    @property
    def contributed_share(self) -> float:
        """Summed soft weight of the harnesses that cleared both gates."""
        return math.fsum(
            outcome.weight for outcome in self.outcomes if outcome.contributed
        )

    @property
    def forfeited_share(self) -> float:
        """Summed soft weight of the harnesses that forfeited their slice."""
        return math.fsum(
            outcome.weight for outcome in self.outcomes if not outcome.contributed
        )


def classify_harness_result(result: RouterHarnessResult) -> RouterFailureStage | None:
    """Fail-closed stage for one reported harness; ``None`` iff it contributed.

    A slice contributes only when the router powered the harness **and** the run
    cleared the floor. ``floor_pass`` without ``operational`` is contract-
    impossible (the floor cannot pass if the harness never ran), so it is flagged
    as a runtime/telemetry anomaly rather than trusted.
    """
    if result.operational and result.floor_pass:
        return None
    if result.floor_pass and not result.operational:
        return RouterFailureStage.HARNESS_RUNTIME
    if not result.operational:
        return RouterFailureStage.HARNESS_OPERATIONAL
    return RouterFailureStage.CORRECTNESS_FLOOR


def classify_router_entry(
    entry: RouterLedgerEntry,
    *,
    expected: Sequence[RouterHarness] = tuple(RouterHarness),
    weights: Mapping[RouterHarness, float] = DEFAULT_HARNESS_WEIGHTS,
) -> RouterFailureReport:
    """Classify every expected harness slice for one published ledger entry.

    ``expected`` is the harness set the track requires (all four in v1). A harness
    the scorer never reported is treated as a ``TELEMETRY`` forfeit (fail-closed).
    When nothing was operational anywhere, the dominant stage is ``ROUTER_BOOT``
    (the router itself never served); otherwise the dominant stage is the stage of
    the largest forfeited slice, so the log leads with the costliest failure.
    """
    reported = {result.harness: result for result in entry.harnesses}
    outcomes: list[RouterHarnessOutcome] = []
    for harness in expected:
        weight = float(weights.get(harness, 0.0))
        result = reported.get(harness)
        if result is None:
            outcomes.append(
                RouterHarnessOutcome(
                    harness, False, RouterFailureStage.TELEMETRY, weight
                )
            )
            continue
        stage = classify_harness_result(result)
        outcomes.append(RouterHarnessOutcome(harness, stage is None, stage, weight))

    if not any(result.operational for result in entry.harnesses):
        dominant: RouterFailureStage | None = RouterFailureStage.ROUTER_BOOT
    else:
        forfeited = [outcome for outcome in outcomes if not outcome.contributed]
        # Largest forfeited slice first; ``max`` keeps ``expected`` order on ties.
        dominant = (
            max(forfeited, key=lambda outcome: outcome.weight).stage
            if forfeited
            else None
        )

    return RouterFailureReport(entry.miner_hotkey, tuple(outcomes), dominant)


__all__ = [
    "DEFAULT_HARNESS_WEIGHTS",
    "RouterFailureReport",
    "RouterFailureStage",
    "RouterHarnessOutcome",
    "classify_harness_result",
    "classify_router_entry",
]
