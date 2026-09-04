"""Adversarial calibration baselines for Coding Memory v2 shadow scoring."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ditto.validator.coding_counterfactual_terminal import (
    Condition,
    CounterfactualTerminalAggregate,
    CounterfactualTerminalResult,
    aggregate_counterfactual_results,
)

_CONDITIONS: tuple[Condition, ...] = ("v0", "v1", "v2", "v3", "v4")


@dataclass(frozen=True)
class CounterfactualBaselineAudit:
    honest: CounterfactualTerminalAggregate
    v0_sandbagging: CounterfactualTerminalAggregate
    stale_follower: CounterfactualTerminalAggregate
    context_stuffer: CounterfactualTerminalAggregate
    missing_control: CounterfactualTerminalAggregate


def audit_adversarial_baselines() -> CounterfactualBaselineAudit:
    """Prove diagnostic lift cannot reward a V0 sandbagging baseline."""

    def aggregate(
        group: str, solved: set[Condition]
    ) -> CounterfactualTerminalAggregate:
        return aggregate_counterfactual_results(
            tuple(
                CounterfactualTerminalResult(group, condition, condition in solved)
                for condition in _CONDITIONS
            ),
            expected_group_ids=(group,),
        )

    honest = aggregate("honest", {"v0", "v1", "v2", "v3", "v4"})
    v0_sandbagging = aggregate("sandbag", {"v1", "v2", "v3", "v4"})
    stale_follower = aggregate("stale", {"v0", "v1", "v2", "v4"})
    context_stuffer = aggregate("stuff", {"v0", "v1", "v4"})
    missing_control = aggregate_counterfactual_results(
        tuple(
            CounterfactualTerminalResult("missing", condition, True)
            for condition in _CONDITIONS[1:]
        ),
        expected_group_ids=("missing",),
    )
    if not (
        v0_sandbagging.useful_lift > honest.useful_lift
        and honest.monotone_shadow_score > v0_sandbagging.monotone_shadow_score
        and honest.monotone_shadow_score > stale_follower.monotone_shadow_score
        and honest.monotone_shadow_score > context_stuffer.monotone_shadow_score
        and honest.monotone_shadow_score > missing_control.monotone_shadow_score
    ):
        raise AssertionError("Coding Memory v2 baseline incentive audit failed")
    _assert_exhaustive_monotonicity(aggregate)
    return CounterfactualBaselineAudit(
        honest=honest,
        v0_sandbagging=v0_sandbagging,
        stale_follower=stale_follower,
        context_stuffer=context_stuffer,
        missing_control=missing_control,
    )


def _assert_exhaustive_monotonicity(
    aggregate: Callable[[str, set[Condition]], CounterfactualTerminalAggregate],
) -> None:
    for mask in range(1 << len(_CONDITIONS)):
        solved = {
            condition
            for index, condition in enumerate(_CONDITIONS)
            if mask & (1 << index)
        }
        baseline = aggregate(f"monotone-{mask}", solved).monotone_shadow_score
        for condition in set(_CONDITIONS) - solved:
            improved = aggregate(
                f"monotone-{mask}-{condition}", solved | {condition}
            ).monotone_shadow_score
            if improved < baseline:
                raise AssertionError("Coding Memory v2 score is not monotone")
