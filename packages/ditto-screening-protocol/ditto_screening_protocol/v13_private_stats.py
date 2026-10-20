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
from ditto_screening_protocol.v13_private_package import (
    V13_PRIVATE_PROFILE,
    V13_PRIVATE_PROFILE_SHA256,
)

_REQUIRED_CLASSES = frozenset(
    {"field_entity_rename", "request_paraphrase", "record_reorder_decoy"}
)
_CATALOG_CLASS = "catalog_reorder_alias"
_ALPHA = 1 - V13_PRIVATE_PROFILE.confidence_level_bps / 10_000
_MATERIAL = V13_PRIVATE_PROFILE.material_degradation_bps / 10_000
_LOWER_THRESHOLD = V13_PRIVATE_PROFILE.confidence_lower_bound_bps / 10_000
_CLEAN_MAX = V13_PRIVATE_PROFILE.clean_control_max_degradation_bps / 10_000
_CELL_PAIRS = V13_PRIVATE_PROFILE.pairs_per_class_per_seed
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
    pair_independence_verified: bool
    seed_replication: bool
    primary_supported: bool
    classes: tuple[PrivateClassAnalysis, ...]


def _one_sided_t_crit(df: int) -> float:
    if df < 1:
        raise PrivateStatisticalUnavailable("private sample is too small")
    return _Z_95 + (_Z_95**3 + _Z_95) / (4 * df)


def _scores(row: PrivatePairCounts) -> tuple[int, int, int]:
    ties = row.pairs - row.control_only_correct - row.variant_only_correct
    both_correct = row.control_correct - row.control_only_correct
    if (
        row.pairs < 2
        or row.control_correct > row.pairs
        or row.variant_correct > row.pairs
        or row.control_only_correct + row.variant_only_correct > row.pairs
        or row.control_correct - row.variant_correct
        != row.control_only_correct - row.variant_only_correct
        or not 0 <= both_correct <= ties
    ):
        raise PrivateStatisticalUnavailable("private pair aggregate is inconsistent")
    plus = row.control_only_correct
    minus = row.variant_only_correct
    return plus, minus, ties


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
        # A constant finite sample does not prove a constant population. The
        # t interval is undefined; report the conservative outcome-space floor
        # instead of claiming zero uncertainty (including for an all-+1 run).
        return n, mean, -1.0
    z = _z_for_alpha(alpha)
    crit = z + (z**3 + z) / (4 * (n - 1))
    return n, mean, mean - crit * math.sqrt(variance / n)


def _one_sided_t(plus: int, minus: int, ties: int, null: float = 0.0) -> float:
    n, mean, variance = _mean_and_variance(plus, minus, ties)
    if variance == 0:
        return -math.inf
    return (mean - null) / math.sqrt(variance / n)


def _holm_lower_bounds(
    counts: dict[str, tuple[int, int, int]],
) -> dict[str, float]:
    """Invert the complete step-down test, re-ranking at each common null.

    A rank-specific interval alone is not a Holm interval: rejection must
    stop at the first failed hypothesis. Inversion also accounts for ranks
    changing as the tested effect changes when standard errors differ.
    """

    def rejected(null: float) -> set[str]:
        ordered = sorted(
            counts, key=lambda name: (-_one_sided_t(*counts[name], null), name)
        )
        supported: set[str] = set()
        for rank, name in enumerate(ordered):
            lower = _lower_bound(*counts[name], _ALPHA / (len(ordered) - rank))[2]
            if lower <= null:
                break
            supported.add(name)
        return supported

    bounds: dict[str, float] = {}
    for name in counts:
        lo, hi = -1.0, 1.0
        for _ in range(48):
            mid = (lo + hi) / 2
            if name in rejected(mid):
                lo = mid
            else:
                hi = mid
        bounds[name] = lo
    return bounds


def _completed_pairs(result: PrivateExecutionResult) -> None:
    if sum(row.pairs for row in result.aggregates) != result.summary.completed_pairs:
        raise PrivateStatisticalUnavailable("private completed-pair count mismatch")


def _group(result: PrivateExecutionResult) -> dict[str, dict[str, PrivatePairCounts]]:
    grouped: dict[str, dict[str, PrivatePairCounts]] = defaultdict(dict)
    for row in result.aggregates:
        if row.pairs != _CELL_PAIRS:
            raise PrivateStatisticalUnavailable("private cell size unavailable")
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
    *,
    target: PrivateExecutionResult,
    clean_control: PrivateExecutionResult,
    pair_independence_verified: bool = False,
) -> PrivateStatisticalAnalysis:
    """Report-only t analysis conditional on a verified independent-pair design.

    Aggregate counts cannot establish independence. Two seed commitments and
    matching effect signs do not prove it either. Without design verification,
    retain descriptive effects but withhold inferential support and bounds.
    """
    if (
        target.summary.profile_sha256 != V13_PRIVATE_PROFILE_SHA256
        or clean_control.summary.profile_sha256 != V13_PRIVATE_PROFILE_SHA256
        or target.summary.manifest_sha256 != clean_control.summary.manifest_sha256
        or target.summary.status != "completed"
        or clean_control.summary.status != "completed"
    ):
        raise PrivateStatisticalUnavailable("private run identity mismatch")
    _completed_pairs(target)
    _completed_pairs(clean_control)
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
    class_counts = {
        name: _add(list(rows.values())) for name, rows in target_rows.items()
    }
    holm_bounds = (
        _holm_lower_bounds(class_counts)
        if pair_independence_verified
        else dict.fromkeys(class_counts, -1.0)
    )
    classes: list[PrivateClassAnalysis] = []
    for name, counts in class_counts.items():
        class_n, class_effect, _variance = _mean_and_variance(*counts)
        classes.append(
            PrivateClassAnalysis(
                transformation_class=name,
                pairs=class_n,
                effect_bps=_bps(class_effect),
                holm_lower_bound_bps=_bps(holm_bounds[name]),
                exploratory=True,
            )
        )
    supported = (
        pair_independence_verified
        and effect >= _MATERIAL
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
        primary_lower_bound_bps=_bps(lower) if pair_independence_verified else -10_000,
        clean_effect_bps=_bps(clean_effect),
        pair_independence_verified=pair_independence_verified,
        seed_replication=seed_replication,
        primary_supported=supported,
        classes=tuple(sorted(classes, key=lambda row: row.transformation_class)),
    )
