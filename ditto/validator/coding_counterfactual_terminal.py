"""Monotone shadow aggregation for Coding Memory v2 evidence."""

from __future__ import annotations

from dataclasses import dataclass
from statistics import mean
from typing import Literal

Condition = Literal["v0", "v1", "v2", "v3", "v4"]
_WEIGHTS: dict[Condition, int] = {"v0": 15, "v1": 30, "v2": 15, "v3": 20, "v4": 20}


@dataclass(frozen=True)
class CounterfactualTerminalResult:
    group_id: str
    condition: Condition
    resolved: bool
    trusted: bool = True


@dataclass(frozen=True)
class CounterfactualTerminalAggregate:
    p0: float
    p1: float
    p2: float
    p3: float
    p4: float
    useful_lift: float
    stale_delta: float
    irrelevant_delta: float
    absolute_condition_score: float
    selective_group_success: float
    monotone_shadow_score: float


def aggregate_counterfactual_results(
    results: tuple[CounterfactualTerminalResult, ...],
) -> CounterfactualTerminalAggregate:
    """Aggregate only complete trusted groups; diagnostics never affect reward."""

    groups: dict[str, dict[Condition, bool]] = {}
    for result in results:
        if not result.trusted or not result.group_id:
            continue
        group = groups.setdefault(result.group_id, {})
        if result.condition in group:
            raise ValueError("duplicate counterfactual result")
        group[result.condition] = result.resolved
    complete = [group for group in groups.values() if set(group) == set(_WEIGHTS)]
    if not complete:
        raise ValueError("no complete trusted counterfactual groups")
    rates = {
        condition: mean(float(group[condition]) for group in complete)
        for condition in _WEIGHTS
    }
    absolute = (
        sum(rates[condition] * weight for condition, weight in _WEIGHTS.items()) / 100
    )
    selective = mean(
        float(all(group[condition] for condition in ("v1", "v2", "v3", "v4")))
        for group in complete
    )
    return CounterfactualTerminalAggregate(
        p0=rates["v0"],
        p1=rates["v1"],
        p2=rates["v2"],
        p3=rates["v3"],
        p4=rates["v4"],
        useful_lift=rates["v1"] - rates["v0"],
        stale_delta=rates["v3"] - rates["v0"],
        irrelevant_delta=rates["v2"] - rates["v0"],
        absolute_condition_score=absolute,
        selective_group_success=selective,
        monotone_shadow_score=0.85 * absolute + 0.15 * selective,
    )
