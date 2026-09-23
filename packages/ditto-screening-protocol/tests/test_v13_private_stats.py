"""Synthetic tests for predeclared, report-only V13 private statistics."""

from __future__ import annotations

from uuid import NAMESPACE_URL, uuid5

import pytest

from ditto_screening_protocol.v13_private_execute import (
    PrivateExecutionResult,
    PrivatePairCounts,
)
from ditto_screening_protocol.v13_private_package import (
    V13_PRIVATE_PROFILE_SHA256,
    V13PrivateRunSummary,
)
from ditto_screening_protocol.v13_private_stats import (
    PrivateStatisticalUnavailable,
    analyze_v13_private_pairs,
)

CLASSES = (
    "field_entity_rename",
    "request_paraphrase",
    "record_reorder_decoy",
)
SEEDS = ("1" * 64, "2" * 64)


def _result(
    *,
    image: str,
    degradation: dict[tuple[str, str], int] | None = None,
    status: str = "completed",
) -> PrivateExecutionResult:
    degradation = degradation or {}
    rows = tuple(
        PrivatePairCounts(
            transformation_class=name,
            seed_commitment=seed,
            pairs=10,
            control_correct=10,
            variant_correct=10 - degradation.get((name, seed), 0),
            control_only_correct=degradation.get((name, seed), 0),
            variant_only_correct=0,
        )
        for name in CLASSES
        for seed in SEEDS
    )
    return PrivateExecutionResult(
        summary=V13PrivateRunSummary(
            agent_id=uuid5(NAMESPACE_URL, f"synthetic-agent-{image}"),
            attempt_id=uuid5(NAMESPACE_URL, f"synthetic-attempt-{image}"),
            artifact_sha256="a" * 64,
            image_sha256=image,
            profile_sha256=V13_PRIVATE_PROFILE_SHA256,
            manifest_sha256="c" * 64,
            runner_hotkey="synthetic-runner",
            status=status,
            completed_pairs=60,
            evidence_sha256="d" * 64,
        ),
        aggregates=rows,
    )


def test_strong_replicated_effect_passes_conservative_holm_bound() -> None:
    target = _result(
        image="b" * 64,
        degradation={(CLASSES[0], seed): 10 for seed in SEEDS},
    )
    analysis = analyze_v13_private_pairs(
        target=target, clean_control=_result(image="e" * 64)
    )
    assert analysis.status == "report_only_candidate_effect"
    finding = next(
        row for row in analysis.classes if row.transformation_class == CLASSES[0]
    )
    assert finding.effect_bps == 10_000
    assert finding.corrected_lower_bound_bps > 500
    assert finding.holm_significant and finding.seed_replication
    assert not any(
        row.evidence_of_material_degradation
        for row in analysis.classes
        if row.transformation_class != CLASSES[0]
    )


def test_marginal_or_single_seed_effect_is_not_established() -> None:
    marginal = {(CLASSES[0], seed): 2 for seed in SEEDS}
    single_seed = {(CLASSES[0], SEEDS[0]): 10}
    # A 60pp effect would cross an uncorrected Hoeffding lower bound, but
    # does not cross the predeclared three-class Holm threshold at n=20.
    uncorrected_only = {(CLASSES[0], seed): 6 for seed in SEEDS}
    for degradation in (marginal, single_seed, uncorrected_only):
        analysis = analyze_v13_private_pairs(
            target=_result(image="b" * 64, degradation=degradation),
            clean_control=_result(image="e" * 64),
        )
        assert analysis.status == "report_only_inconclusive"


def test_degraded_clean_control_blocks_candidate_effect() -> None:
    target = _result(
        image="b" * 64,
        degradation={(CLASSES[0], seed): 10 for seed in SEEDS},
    )
    clean = _result(
        image="e" * 64,
        degradation={(CLASSES[0], SEEDS[0]): 1},
    )
    analysis = analyze_v13_private_pairs(target=target, clean_control=clean)
    assert analysis.status == "report_only_inconclusive"


def test_missing_or_inconsistent_controls_are_inconclusive() -> None:
    target = _result(image="b" * 64)
    with pytest.raises(PrivateStatisticalUnavailable, match="provenance"):
        analyze_v13_private_pairs(target=target, clean_control=target)
    with pytest.raises(PrivateStatisticalUnavailable, match="provenance"):
        analyze_v13_private_pairs(
            target=target,
            clean_control=_result(image="e" * 64, status="inconclusive"),
        )
    bad = target.model_copy(
        update={
            "aggregates": target.aggregates[:-1],
        }
    )
    with pytest.raises(PrivateStatisticalUnavailable, match="seed coverage"):
        analyze_v13_private_pairs(target=bad, clean_control=_result(image="e" * 64))
    inconsistent = target.aggregates[0].model_copy(update={"variant_correct": 8})
    bad = target.model_copy(
        update={"aggregates": (inconsistent, *target.aggregates[1:])}
    )
    with pytest.raises(PrivateStatisticalUnavailable, match="inconsistent"):
        analyze_v13_private_pairs(target=bad, clean_control=_result(image="e" * 64))
