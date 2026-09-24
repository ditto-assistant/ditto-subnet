"""Coverage and power of the predeclared V13 pooled paired-effect plan."""

from __future__ import annotations

import random
from uuid import UUID

import pytest

from ditto_screening_protocol.v13_private_execute import (
    PrivateExecutionResult,
    PrivatePairCounts,
)
from ditto_screening_protocol.v13_private_package import V13PrivateRunSummary
from ditto_screening_protocol.v13_private_stats import (
    PrivateStatisticalUnavailable,
    analyze_v13_private_pairs,
)

_SEED_A = "a" * 64
_SEED_B = "b" * 64
_CLASSES = (
    "field_entity_rename",
    "request_paraphrase",
    "record_reorder_decoy",
)


def _summary(pairs: int) -> V13PrivateRunSummary:
    return V13PrivateRunSummary(
        agent_id=UUID(int=1),
        attempt_id=UUID(int=2),
        artifact_sha256="1" * 64,
        image_sha256="2" * 64,
        profile_sha256="3" * 64,
        manifest_sha256="4" * 64,
        runner_hotkey="runner",
        status="completed",
        completed_pairs=pairs,
        evidence_sha256="5" * 64,
    )


def _row(
    class_name: str, seed: str, plus: int, minus: int, ties: int
) -> PrivatePairCounts:
    pairs = plus + minus + ties
    return PrivatePairCounts(
        transformation_class=class_name,
        seed_commitment=seed,
        pairs=pairs,
        control_correct=plus + ties // 2,
        variant_correct=minus + ties // 2,
        control_only_correct=plus,
        variant_only_correct=minus,
    )


def _result(
    effect_rows: list[tuple[str, str, int, int, int]],
) -> PrivateExecutionResult:
    rows = [
        _row(class_name, seed, plus, minus, ties)
        for class_name, seed, plus, minus, ties in effect_rows
    ]
    return PrivateExecutionResult(
        summary=_summary(sum(row.pairs for row in rows)), aggregates=tuple(rows)
    )


def _balanced(plus: int, minus: int, ties: int) -> PrivateExecutionResult:
    rows = [
        (class_name, seed, plus, minus, ties)
        for class_name in _CLASSES
        for seed in (_SEED_A, _SEED_B)
    ]
    return _result(rows)


def test_primary_support_is_report_only_and_needs_both_seeds_and_clean_control() -> (
    None
):
    target = _balanced(plus=4, minus=0, ties=6)
    clean = _balanced(plus=0, minus=0, ties=10)
    report = analyze_v13_private_pairs(target=target, clean_control=clean)
    assert report.status == "report_only_primary_supported"
    assert report.primary_supported is True
    assert report.primary_pairs == 60
    assert report.primary_effect_bps >= 1500
    assert report.primary_lower_bound_bps > 500
    assert "clear" not in report.status
    assert "reject" not in report.status


def test_weak_or_one_seed_effect_stays_inconclusive() -> None:
    weak = _balanced(plus=2, minus=1, ties=7)
    clean = _balanced(plus=0, minus=0, ties=10)
    assert (
        analyze_v13_private_pairs(target=weak, clean_control=clean).primary_supported
        is False
    )
    rows = [(class_name, _SEED_A, 4, 0, 6) for class_name in _CLASSES] + [
        (class_name, _SEED_B, 0, 1, 9) for class_name in _CLASSES
    ]
    opposite = analyze_v13_private_pairs(target=_result(rows), clean_control=clean)
    assert opposite.seed_replication is False
    assert opposite.primary_supported is False


def test_inconsistent_counts_fail_closed() -> None:
    bad = _balanced(plus=4, minus=0, ties=6)
    broken = bad.aggregates[0].model_copy(update={"variant_only_correct": 3})
    with pytest.raises(PrivateStatisticalUnavailable):
        analyze_v13_private_pairs(
            target=PrivateExecutionResult(
                summary=bad.summary, aggregates=(broken, *bad.aggregates[1:])
            ),
            clean_control=_balanced(plus=0, minus=0, ties=10),
        )


def _draw(
    rng: random.Random, n: int, p_plus: float, p_minus: float
) -> tuple[int, int, int]:
    plus = minus = 0
    for _ in range(n):
        roll = rng.random()
        if roll < p_plus:
            plus += 1
        elif roll < p_plus + p_minus:
            minus += 1
    return plus, minus, n - plus - minus


def test_primary_interval_covers_and_has_power_at_the_published_sample() -> None:
    """Fixed-seed simulation of the predeclared one-sided t lower bound."""
    from ditto_screening_protocol.v13_private_stats import _lower_bound

    rng = random.Random(2163)
    reps = 400
    covered = 0
    for _ in range(reps):
        p_tie = rng.choice((0.4, 0.7, 0.9))
        p_plus = (1 - p_tie) / 2
        plus, minus, ties = _draw(rng, 60, p_plus, p_plus)
        _n, _mean, lower = _lower_bound(plus, minus, ties, 0.05)
        covered += int(lower <= 0.0)
    assert covered / reps >= 0.93

    powered = 0
    for _ in range(reps):
        plus, minus, ties = _draw(rng, 60, 0.30, 0.05)
        _n, mean, lower = _lower_bound(plus, minus, ties, 0.05)
        powered += int(mean >= 0.15 and lower > 0.05)
    assert powered / reps >= 0.80
