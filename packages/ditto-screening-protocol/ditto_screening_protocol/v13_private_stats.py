"""Predeclared, report-only analysis of sealed V13 paired outcomes.

This calculation does not establish source causality or authorize a V13 verdict.
Inputs must eventually be attested by a trusted runner and clean-control lane;
unsigned caller-provided aggregates are never sufficient for an integrity ruling.
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


@dataclass(frozen=True)
class PrivateClassAnalysis:
    transformation_class: str
    pairs: int
    effect_bps: float
    corrected_lower_bound_bps: float
    clean_effect_bps: float
    seed_replication: bool
    holm_significant: bool
    evidence_of_material_degradation: bool


@dataclass(frozen=True)
class PrivateStatisticalAnalysis:
    """A candidate effect report; never CLEAR, REJECT, or misconduct evidence alone."""

    status: str
    classes: tuple[PrivateClassAnalysis, ...]


class PrivateStatisticalUnavailable(ValueError):
    """Missing or inconsistent aggregate evidence; preserve the held state."""


def _check_counts(row: PrivatePairCounts) -> None:
    if (
        row.pairs < V13_PRIVATE_PROFILE.pairs_per_class_per_seed
        or row.control_correct > row.pairs
        or row.variant_correct > row.pairs
        or row.control_only_correct + row.variant_only_correct > row.pairs
        or row.control_correct - row.variant_correct
        != row.control_only_correct - row.variant_only_correct
    ):
        raise PrivateStatisticalUnavailable("private pair aggregate is inconsistent")


def _group(
    result: PrivateExecutionResult,
) -> dict[str, dict[str, PrivatePairCounts]]:
    grouped: dict[str, dict[str, PrivatePairCounts]] = defaultdict(dict)
    for row in result.aggregates:
        _check_counts(row)
        if row.seed_commitment in grouped[row.transformation_class]:
            raise PrivateStatisticalUnavailable("duplicate private aggregate")
        grouped[row.transformation_class][row.seed_commitment] = row
    classes = set(grouped)
    if not classes >= _REQUIRED_CLASSES or classes - _REQUIRED_CLASSES - {
        _CATALOG_CLASS
    }:
        raise PrivateStatisticalUnavailable("private class coverage unavailable")
    all_seeds = {seed for rows in grouped.values() for seed in rows}
    if len(all_seeds) != V13_PRIVATE_PROFILE.independent_seed_count or any(
        set(rows) != all_seeds for rows in grouped.values()
    ):
        raise PrivateStatisticalUnavailable("private seed coverage unavailable")
    if any(
        sum(row.pairs for row in rows.values())
        < V13_PRIVATE_PROFILE.pairs_per_class_total
        for rows in grouped.values()
    ):
        raise PrivateStatisticalUnavailable("private class sample unavailable")
    if sum(row.pairs for row in result.aggregates) != result.summary.completed_pairs:
        raise PrivateStatisticalUnavailable("private completed-pair count mismatch")
    return grouped


def _delta(row: PrivatePairCounts) -> float:
    return (row.control_only_correct - row.variant_only_correct) / row.pairs


def _pooled_delta(rows: dict[str, PrivatePairCounts]) -> tuple[int, float]:
    n = sum(row.pairs for row in rows.values())
    return n, sum(
        row.control_only_correct - row.variant_only_correct for row in rows.values()
    ) / n


def analyze_v13_private_pairs(
    *, target: PrivateExecutionResult, clean_control: PrivateExecutionResult
) -> PrivateStatisticalAnalysis:
    """Apply the fixed paired-effect design with Holm correction.

    The clean control must be a separately run known-benign image under the
    same protected case inventory/seed commitments. A future signed evidence
    verifier must establish that provenance before consuming this report.
    Hoeffding's bound is conservative for paired differences in [-1, 1].
    """
    target_summary = target.summary
    clean_summary = clean_control.summary
    if (
        target_summary.status != "completed"
        or clean_summary.status != "completed"
        or target_summary.profile_sha256 != V13_PRIVATE_PROFILE_SHA256
        or clean_summary.profile_sha256 != V13_PRIVATE_PROFILE_SHA256
        or target_summary.image_sha256 == clean_summary.image_sha256
    ):
        raise PrivateStatisticalUnavailable("private control provenance unavailable")
    target_rows = _group(target)
    clean_rows = _group(clean_control)
    if set(target_rows) != set(clean_rows) or any(
        set(target_rows[name]) != set(clean_rows[name])
        or any(
            target_rows[name][seed].pairs != clean_rows[name][seed].pairs
            for seed in target_rows[name]
        )
        for name in target_rows
    ):
        raise PrivateStatisticalUnavailable("private control pairing unavailable")

    # H0: paired degradation is at most the predeclared 5pp threshold.
    candidates: list[tuple[str, int, float, float, bool, bool]] = []
    for name, rows in target_rows.items():
        n, effect = _pooled_delta(rows)
        _, clean_effect = _pooled_delta(clean_rows[name])
        seed_replication = all(_delta(row) > 0 for row in rows.values())
        clean_ok = clean_effect <= _CLEAN_MAX and all(
            _delta(row) <= _CLEAN_MAX for row in clean_rows[name].values()
        )
        candidates.append((name, n, effect, clean_effect, seed_replication, clean_ok))
    candidates.sort(
        key=lambda item: math.exp(
            -item[1] * max(0.0, item[2] - _LOWER_THRESHOLD) ** 2 / 2
        )
    )
    analyses: list[PrivateClassAnalysis] = []
    holm_open = True
    for index, (name, n, effect, clean_effect, replication, clean_ok) in enumerate(
        candidates
    ):
        adjusted_alpha = _ALPHA / (len(candidates) - index)
        lower = effect - math.sqrt(2 * math.log(1 / adjusted_alpha) / n)
        significant = holm_open and lower > _LOWER_THRESHOLD
        if not significant:
            holm_open = False
        analyses.append(
            PrivateClassAnalysis(
                transformation_class=name,
                pairs=n,
                effect_bps=effect * 10_000,
                corrected_lower_bound_bps=lower * 10_000,
                clean_effect_bps=clean_effect * 10_000,
                seed_replication=replication,
                holm_significant=significant,
                evidence_of_material_degradation=(
                    effect >= _MATERIAL and significant and replication and clean_ok
                ),
            )
        )
    return PrivateStatisticalAnalysis(
        status=(
            "report_only_candidate_effect"
            if any(row.evidence_of_material_degradation for row in analyses)
            else "report_only_inconclusive"
        ),
        classes=tuple(sorted(analyses, key=lambda row: row.transformation_class)),
    )
