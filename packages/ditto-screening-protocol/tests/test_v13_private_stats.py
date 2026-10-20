"""Coverage and power of the predeclared V13 pooled paired-effect plan."""

from __future__ import annotations

import random
from uuid import UUID

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

_SEED_A = "a" * 64
_SEED_B = "b" * 64
_CLASSES = (
    "field_entity_rename",
    "request_paraphrase",
    "record_reorder_decoy",
)


def _analyze(**kwargs):
    # These synthetic fixtures model independently sampled pairs. Real callers
    # must verify their case-bank design rather than infer it from two seeds.
    return analyze_v13_private_pairs(pair_independence_verified=True, **kwargs)


def _summary(pairs: int) -> V13PrivateRunSummary:
    return V13PrivateRunSummary(
        agent_id=UUID(int=1),
        attempt_id=UUID(int=2),
        artifact_sha256="1" * 64,
        image_sha256="2" * 64,
        profile_sha256=V13_PRIVATE_PROFILE_SHA256,
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
    report = _analyze(target=target, clean_control=clean)
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
    assert _analyze(target=weak, clean_control=clean).primary_supported is False
    rows = [(class_name, _SEED_A, 4, 0, 6) for class_name in _CLASSES] + [
        (class_name, _SEED_B, 0, 1, 9) for class_name in _CLASSES
    ]
    opposite = _analyze(target=_result(rows), clean_control=clean)
    assert opposite.seed_replication is False
    assert opposite.primary_supported is False


def test_uneven_cells_that_still_total_60_fail_closed() -> None:
    """A pooled floor of 60 is not a substitute for 10 pairs in every cell."""
    specs = {
        ("field_entity_rename", _SEED_A): (6, 0, 10),
        ("field_entity_rename", _SEED_B): (2, 0, 2),
    }
    coded = [
        (class_name, seed, *specs.get((class_name, seed), (4, 0, 6)))
        for class_name in _CLASSES
        for seed in (_SEED_A, _SEED_B)
    ]
    assert sum(plus + minus + ties for _name, _seed, plus, minus, ties in coded) == 60
    with pytest.raises(PrivateStatisticalUnavailable, match="cell size"):
        _analyze(
            target=_result(coded),
            clean_control=_balanced(plus=0, minus=0, ties=10),
        )


def test_completed_count_and_profile_digest_fail_closed() -> None:
    target = _balanced(plus=4, minus=0, ties=6)
    clean = _balanced(plus=0, minus=0, ties=10)
    wrong_count = target.model_copy(
        update={"summary": target.summary.model_copy(update={"completed_pairs": 61})}
    )
    with pytest.raises(PrivateStatisticalUnavailable, match="completed-pair"):
        _analyze(target=wrong_count, clean_control=clean)
    wrong_profile = target.model_copy(
        update={
            "summary": target.summary.model_copy(update={"profile_sha256": "f" * 64})
        }
    )
    with pytest.raises(PrivateStatisticalUnavailable, match="identity"):
        _analyze(target=wrong_profile, clean_control=clean)


def test_holm_bounds_invert_the_full_step_down_test() -> None:
    """Every positive class bound must survive all preceding Holm steps."""
    from ditto_screening_protocol.v13_private_stats import _lower_bound, _one_sided_t

    noisy = ("field_entity_rename", 5, 2, 3)  # pooled effect 0.30, wide variance
    tight = ("request_paraphrase", 2, 0, 8)  # pooled effect 0.20, small variance
    quiet = ("record_reorder_decoy", 0, 0, 10)
    cells = {noisy[0]: noisy[1:], tight[0]: tight[1:], quiet[0]: quiet[1:]}
    rows = [
        (name, seed, plus, minus, ties)
        for name, (plus, minus, ties) in cells.items()
        for seed in (_SEED_A, _SEED_B)
    ]
    report = _analyze(
        target=_result(rows), clean_control=_balanced(plus=0, minus=0, ties=10)
    )
    by_name = {row.transformation_class: row for row in report.classes}
    assert by_name[noisy[0]].effect_bps > by_name[tight[0]].effect_bps
    counts = {name: (2 * p, 2 * m, 2 * t) for name, (p, m, t) in cells.items()}
    for null in (-0.1, 0.0, 0.01, 0.1, 0.2):
        ordered = sorted(
            counts, key=lambda name: (-_one_sided_t(*counts[name], null), name)
        )
        rejected = set()
        for rank, name in enumerate(ordered):
            if _lower_bound(*counts[name], 0.05 / (len(ordered) - rank))[2] <= null:
                break
            rejected.add(name)
        assert {
            name
            for name, row in by_name.items()
            if row.holm_lower_bound_bps > null * 10_000
        } == rejected


def test_holm_stops_after_the_first_failed_rank_including_ties() -> None:
    report = _analyze(target=_balanced(2, 0, 8), clean_control=_balanced(0, 0, 10))
    assert all(row.holm_lower_bound_bps <= 0 for row in report.classes)
    assert len({row.holm_lower_bound_bps for row in report.classes}) == 1


@pytest.mark.parametrize("counts", [(60, 0, 0), (0, 60, 0), (0, 0, 60)])
def test_constant_samples_do_not_claim_zero_uncertainty(counts) -> None:
    from ditto_screening_protocol.v13_private_stats import _lower_bound

    assert _lower_bound(*counts, 0.05)[2] == -1.0


def test_all_positive_finite_sample_stays_inconclusive() -> None:
    report = _analyze(target=_balanced(10, 0, 0), clean_control=_balanced(0, 0, 10))
    assert report.primary_effect_bps == 10_000
    assert report.primary_lower_bound_bps == -10_000
    assert report.primary_supported is False
    assert all(row.holm_lower_bound_bps == -10_000 for row in report.classes)


def test_seed_replication_does_not_establish_pair_independence() -> None:
    report = analyze_v13_private_pairs(
        target=_balanced(4, 0, 6), clean_control=_balanced(0, 0, 10)
    )
    assert report.seed_replication is True
    assert report.primary_effect_bps == 4000
    assert report.pair_independence_verified is False
    assert report.primary_supported is False
    assert report.primary_lower_bound_bps == -10_000
    assert all(row.holm_lower_bound_bps == -10_000 for row in report.classes)


def test_inconsistent_counts_fail_closed() -> None:
    bad = _balanced(plus=4, minus=0, ties=6)
    broken = bad.aggregates[0].model_copy(update={"variant_only_correct": 3})
    with pytest.raises(PrivateStatisticalUnavailable):
        _analyze(
            target=PrivateExecutionResult(
                summary=bad.summary, aggregates=(broken, *bad.aggregates[1:])
            ),
            clean_control=_balanced(plus=0, minus=0, ties=10),
        )


@pytest.mark.parametrize("side", ["target", "clean_control"])
@pytest.mark.parametrize("control,variant", [(1, 0), (10, 9)])
def test_impossible_both_correct_counts_fail_closed(
    side: str, control: int, variant: int
) -> None:
    valid = _balanced(plus=5, minus=4, ties=1)
    broken = valid.aggregates[0].model_copy(
        update={"control_correct": control, "variant_correct": variant}
    )
    impossible = valid.model_copy(
        update={"aggregates": (broken, *valid.aggregates[1:])}
    )
    inputs = {"target": valid, "clean_control": valid, side: impossible}
    with pytest.raises(PrivateStatisticalUnavailable, match="inconsistent"):
        _analyze(**inputs)


@pytest.mark.parametrize("control,variant", [(5, 4), (6, 5)])
def test_both_correct_can_span_the_tie_count(control: int, variant: int) -> None:
    valid = _balanced(plus=5, minus=4, ties=1)
    boundary = valid.aggregates[0].model_copy(
        update={"control_correct": control, "variant_correct": variant}
    )
    result = valid.model_copy(update={"aggregates": (boundary, *valid.aggregates[1:])})
    assert _analyze(target=result, clean_control=result).primary_pairs == 60


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
