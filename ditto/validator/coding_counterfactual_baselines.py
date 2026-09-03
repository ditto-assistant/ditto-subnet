"""Adversarial calibration baselines for Coding Memory v2 shadow scoring."""

from __future__ import annotations

from dataclasses import dataclass

from ditto.validator.coding_counterfactual_terminal import (
    CounterfactualTerminalAggregate,
    CounterfactualTerminalResult,
    aggregate_counterfactual_results,
)


@dataclass(frozen=True)
class CounterfactualBaselineAudit:
    honest: CounterfactualTerminalAggregate
    v0_sandbagging: CounterfactualTerminalAggregate
    stale_follower: CounterfactualTerminalAggregate


def audit_adversarial_baselines() -> CounterfactualBaselineAudit:
    """Prove diagnostic lift cannot reward a V0 sandbagging baseline."""

    def aggregate(group: str, solved: set[str]) -> CounterfactualTerminalAggregate:
        return aggregate_counterfactual_results(
            tuple(
                CounterfactualTerminalResult(group, condition, condition in solved)
                for condition in ("v0", "v1", "v2", "v3", "v4")
            )  # type: ignore[arg-type]
        )

    honest = aggregate("honest", {"v0", "v1", "v2", "v3", "v4"})
    v0_sandbagging = aggregate("sandbag", {"v1", "v2", "v3", "v4"})
    stale_follower = aggregate("stale", {"v0", "v1", "v2", "v4"})
    if not (
        v0_sandbagging.useful_lift > honest.useful_lift
        and honest.monotone_shadow_score > v0_sandbagging.monotone_shadow_score
        and honest.monotone_shadow_score > stale_follower.monotone_shadow_score
    ):
        raise AssertionError("Coding Memory v2 baseline incentive audit failed")
    return CounterfactualBaselineAudit(
        honest=honest,
        v0_sandbagging=v0_sandbagging,
        stale_follower=stale_follower,
    )
