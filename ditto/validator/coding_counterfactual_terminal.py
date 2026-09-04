"""Monotone shadow aggregation for Coding Memory v2 evidence."""

from __future__ import annotations

from dataclasses import dataclass
from statistics import mean
from typing import Literal

Condition = Literal["v0", "v1", "v2", "v3", "v4"]
_CONDITIONS: tuple[Condition, ...] = ("v0", "v1", "v2", "v3", "v4")
_SELECTIVE_CONDITIONS: tuple[Condition, ...] = ("v1", "v2", "v3", "v4")
_WEIGHTS: dict[Condition, int] = {
    "v0": 15,
    "v1": 30,
    "v2": 15,
    "v3": 20,
    "v4": 20,
}


@dataclass(frozen=True)
class CounterfactualTerminalResult:
    group_id: str
    condition: Condition
    resolved: bool
    trusted: bool = True


@dataclass(frozen=True)
class CounterfactualTerminalAggregate:
    expected_group_count: int
    quarantined_group_count: int
    missing_result_count: int
    untrusted_result_count: int
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
    *,
    expected_group_ids: tuple[str, ...],
    quarantined_group_ids: frozenset[str] = frozenset(),
) -> CounterfactualTerminalAggregate:
    """Score expected groups fail-closed; quarantine requires explicit authority."""

    expected = set(expected_group_ids)
    if (
        not 1 <= len(expected_group_ids) <= 100
        or len(expected) != len(expected_group_ids)
        or any(not group_id for group_id in expected)
        or not quarantined_group_ids <= expected
        or quarantined_group_ids == expected
    ):
        raise ValueError("counterfactual expected-group authority is invalid")
    active = expected - quarantined_group_ids
    groups: dict[str, dict[Condition, bool]] = {group_id: {} for group_id in active}
    untrusted = 0
    for result in results:
        if result.group_id not in expected or result.condition not in _WEIGHTS:
            raise ValueError("counterfactual result is outside expected authority")
        if result.group_id in quarantined_group_ids:
            continue
        group = groups[result.group_id]
        if result.condition in group:
            raise ValueError("duplicate counterfactual result")
        if not result.trusted:
            untrusted += 1
        group[result.condition] = result.resolved if result.trusted else False
    missing = sum(len(_CONDITIONS) - len(group) for group in groups.values())
    rates = {
        condition: mean(float(group.get(condition, False)) for group in groups.values())
        for condition in _CONDITIONS
    }
    absolute = (
        sum(rates[condition] * weight for condition, weight in _WEIGHTS.items()) / 100
    )
    selective = mean(
        float(all(group.get(condition, False) for condition in _SELECTIVE_CONDITIONS))
        for group in groups.values()
    )
    return CounterfactualTerminalAggregate(
        expected_group_count=len(expected),
        quarantined_group_count=len(quarantined_group_ids),
        missing_result_count=missing,
        untrusted_result_count=untrusted,
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
