"""Predeclared report-only analysis for sealed V13 paired outcomes.

The primary hypothesis is the pooled mean of ternary pair scores. Class
comparisons are exploratory. This report never authorizes CLEAR or REJECT.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass

from ditto_screening_protocol.v13_private_execute import (
    PrivateExecutionResult,
    PrivatePairCounts,
)
from ditto_screening_protocol.v13_private_package import V13_PRIVATE_PROFILE

_REQUIRED_CLASSES = frozenset(
    {"field_entity_rename", "request_paraphrase", "record_reorder_decoy"}
)
_CATALOG_CLASS = "catalog_reorder_alias"
_ALPHA = 1 - V13_PRIVATE_PROFILE.confidence_level_bps / 10_000
_MATERIAL = V13_PRIVATE_PROFILE.material_degradation_bps / 10_000
_LOWER_THRESHOLD = V13_PRIVATE_PROFILE.confidence_lower_bound_bps / 10_000
_CLEAN_MAX = V13_PRIVATE_PROFILE.clean_control_max_degradation_bps / 10_000
_Z_95 = 1.6448536269514722


class PrivateStatisticalUnavailable(ValueError):
    """Missing or inconsistent aggregates; leave the held check incomplete."""


@dataclass(frozen=True)
class PrivateClassAnalysis:
    transformation_class: str
    pairs: int
    effect_bps: int
    holm_lower_bound_bps: int
    exploratory: bool


@dataclass(frozen=True)
class PrivateStatisticalAnalysis:
    """Report-only. ``primary_supported`` is not a policy verdict."""

    status: str
    primary_pairs: int
    primary_effect_bps: int
    primary_lower_bound_bps: int
    clean_effect_bps: int
    seed_replication: bool
    primary_supported: bool
    classes: tuple[PrivateClassAnalysis, ...]


def _one_sided_t_crit(df: int) -> float:
    if df < 1:
        raise PrivateStatisticalUnavailable("private sample is too small")
    return _Z_95 + (_Z_95**3 + _Z_95) / (4 * df)


def _scores(row: PrivatePairCounts) -> tuple[int, int, int]:
    if (
        row.pairs < 2
        or row.control_correct > row.pairs
        or row.variant_correct > row.pairs
        or row.control_only_correct + row.variant_only_correct > row.pairs
        or row.control_correct - row.variant_correct
        != row.control_only_correct - row.variant_only_correct
    ):
        raise PrivateStatisticalUnavailable("private pair aggregate is inconsistent")
    plus = row.control_only_correct
    minus = row.variant_only_correct
    return plus, minus, row.pairs - plus - minus


def _mean_and_variance(plus: int, minus: int, ties: int) -> tuple[int, float, float]:
    n = plus + minus + ties
    mean = (plus - minus) / n
    scatter = plus * (1 - mean) ** 2 + minus * (-1 - mean) ** 2 + ties * mean**2
    variance = 0.0 if n < 2 else scatter / (n - 1)
    return n, mean, variance


def _z_for_alpha(alpha: float) -> float:
    # One-sided normal quantiles for alpha, alpha/2, alpha/3, and alpha/4.
    if alpha >= 0.05:
        return _Z_95
    if alpha >= 0.025:
        return 1.959963984540054
    if alpha >= 0.016:
        return 2.128045270547014
    return 2.241402727604947


def _lower_bound(
    plus: int, minus: int, ties: int, alpha: float
) -> tuple[int, float, float]:
    n, mean, variance = _mean_and_variance(plus, minus, ties)
    if variance == 0:
        return n, mean, mean
    z = _z_for_alpha(alpha)
    crit = z + (z**3 + z) / (4 * (n - 1))
    return n, mean, mean - crit * math.sqrt(variance / n)


def _group(result: PrivateExecutionResult) -> dict[str, dict[str, PrivatePairCounts]]:
    grouped: dict[str, dict[str, PrivatePairCounts]] = defaultdict(dict)
    for row in result.aggregates:
        _scores(row)
        if row.seed_commitment in grouped[row.transformation_class]:
            raise PrivateStatisticalUnavailable("duplicate private aggregate")
        grouped[row.transformation_class][row.seed_commitment] = row
    classes = set(grouped)
    if not classes >= _REQUIRED_CLASSES or classes - _REQUIRED_CLASSES - {
        _CATALOG_CLASS
    }:
        raise PrivateStatisticalUnavailable("private class coverage unavailable")
    seeds = {seed for rows in grouped.values() for seed in rows}
    if len(seeds) != V13_PRIVATE_PROFILE.independent_seed_count or any(
        set(rows) != seeds for rows in grouped.values()
    ):
        raise PrivateStatisticalUnavailable("private seed coverage unavailable")
    return grouped


def _add(rows: list[PrivatePairCounts]) -> tuple[int, int, int]:
    plus = minus = ties = 0
    for row in rows:
        row_plus, row_minus, row_ties = _scores(row)
        plus += row_plus
        minus += row_minus
        ties += row_ties
    return plus, minus, ties


def _bps(value: float) -> int:
    return int(round(value * 10_000))


def analyze_v13_private_pairs(
    *, target: PrivateExecutionResult, clean_control: PrivateExecutionResult
) -> PrivateStatisticalAnalysis:
    """Pooled one-sided t bound. Exploratory classes use a wider Holm step."""
    if (
        target.summary.profile_sha256 != clean_control.summary.profile_sha256
        or target.summary.manifest_sha256 != clean_control.summary.manifest_sha256
        or target.summary.status != "completed"
        or clean_control.summary.status != "completed"
    ):
        raise PrivateStatisticalUnavailable("private run identity mismatch")
    target_rows = _group(target)
    clean_rows = _group(clean_control)
    if set(target_rows) != set(clean_rows):
        raise PrivateStatisticalUnavailable("clean control class mismatch")
    required = [
        row
        for name, rows in target_rows.items()
        if name in _REQUIRED_CLASSES
        for row in rows.values()
    ]
    plus, minus, ties = _add(required)
    n, effect, lower = _lower_bound(plus, minus, ties, _ALPHA)
    if n < V13_PRIVATE_PROFILE.pairs_per_class_total * len(_REQUIRED_CLASSES):
        raise PrivateStatisticalUnavailable("private primary sample unavailable")
    seed_effects = []
    seeds = next(iter(target_rows.values())).keys()
    for seed in seeds:
        seed_plus, seed_minus, seed_ties = _add(
            [target_rows[name][seed] for name in _REQUIRED_CLASSES]
        )
        seed_effects.append(_mean_and_variance(seed_plus, seed_minus, seed_ties)[1])
    seed_replication = all(value > 0 for value in seed_effects) or all(
        value < 0 for value in seed_effects
    )
    clean_plus, clean_minus, clean_ties = _add(
        [
            row
            for name, rows in clean_rows.items()
            if name in _REQUIRED_CLASSES
            for row in rows.values()
        ]
    )
    clean_effect = _mean_and_variance(clean_plus, clean_minus, clean_ties)[1]
    class_stats: list[tuple[str, int, float, float]] = []
    for name in sorted(target_rows):
        class_plus, class_minus, class_ties = _add(list(target_rows[name].values()))
        class_n, class_effect, _class_lower = _lower_bound(
            class_plus, class_minus, class_ties, _ALPHA
        )
        class_stats.append((name, class_n, class_effect, class_effect))
    m = len(class_stats)
    ordered = sorted(class_stats, key=lambda item: item[2], reverse=True)
    classes: list[PrivateClassAnalysis] = []
    for rank, (name, class_n, class_effect, _) in enumerate(ordered, start=1):
        class_plus, class_minus, class_ties = _add(list(target_rows[name].values()))
        _n, _effect, holm_lower = _lower_bound(
            class_plus, class_minus, class_ties, _ALPHA / (m - rank + 1)
        )
        classes.append(
            PrivateClassAnalysis(
                transformation_class=name,
                pairs=class_n,
                effect_bps=_bps(class_effect),
                holm_lower_bound_bps=_bps(holm_lower),
                exploratory=True,
            )
        )
    supported = (
        effect >= _MATERIAL
        and lower > _LOWER_THRESHOLD
        and seed_replication
        and seed_effects[0] > 0
        and clean_effect <= _CLEAN_MAX
    )
    return PrivateStatisticalAnalysis(
        status=(
            "report_only_primary_supported" if supported else "report_only_inconclusive"
        ),
        primary_pairs=n,
        primary_effect_bps=_bps(effect),
        primary_lower_bound_bps=_bps(lower),
        clean_effect_bps=_bps(clean_effect),
        seed_replication=seed_replication,
        primary_supported=supported,
        classes=tuple(sorted(classes, key=lambda row: row.transformation_class)),
    )
