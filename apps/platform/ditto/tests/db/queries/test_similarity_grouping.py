"""The grouping rule, against real production sketches.

Synthetic sketches can prove the arithmetic; they cannot prove the *threshold*,
because the whole difficulty of this rule is that every miner builds on the same
public starter kit and a badly-calibrated metric collapses the subnet into one
budget. So the calibration tests run on real fingerprints lifted verbatim from
production (``similarity_fixtures/production_sketches.json``):

* four submissions from the family that motivated the gate, submitted under
  **four different payment coldkeys**, which the existing owner gate therefore
  sees as four unrelated owners;
* four independent miners with long, separate submission lineages.

All eight derive from the same starter kit and share substantial byte-identical
scaffold. If the metric measured raw source they would all group. The assertions
below are the standing proof that it does not.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from ditto.db.queries.similarity_budget import SubmissionSketch
from ditto.db.queries.similarity_grouping import (
    DEFAULT_CONTAINMENT_THRESHOLD,
    DEFAULT_JACCARD_THRESHOLD,
    MIN_COMPARABLE_SHINGLES,
    SimilarityBudgetPolicy,
    same_budget,
    similar_submissions,
    similarity_budgets,
    similarity_scores,
)

_FIXTURES = json.loads(
    (
        Path(__file__).parent / "similarity_fixtures" / "production_sketches.json"
    ).read_text()
)

# The four submissions from the one family, each under a different coldkey.
_FAMILY = ("family_a", "family_b", "family_c", "family_d")
# Four independent miners.
_INDEPENDENT = ("miner_gkat", "miner_kabaw", "miner_lihai", "miner_oraclemind")


def _production(name: str) -> SubmissionSketch:
    entry = _FIXTURES[name]
    return SubmissionSketch(
        agent_id=UUID(entry["agent_id"]), fingerprint=entry["fingerprint"]
    )


def _synthetic(*hashes: str, card: int | None = None) -> SubmissionSketch:
    """A sketch in the exact shape the upload path stores."""
    return SubmissionSketch(
        agent_id=uuid4(),
        fingerprint={
            "v": 2,
            "k": 256,
            "card": card if card is not None else len(hashes),
            "m": list(hashes),
            "corpus": "c0ffee",
        },
    )


def _hashes(start: int, count: int) -> tuple[str, ...]:
    return tuple(f"{value:016x}" for value in range(start, start + count))


# ---------------------------------------------------------------------------
# Calibration -- real production sketches
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("left", _FAMILY)
@pytest.mark.parametrize("right", _FAMILY)
def test_the_family_shares_one_budget_across_four_coldkeys(
    left: str, right: str
) -> None:
    """Near-identical submissions group whichever key paid for them.

    These four are what the gate exists for: one submission lineage, four
    payment coldkeys, so ``owner_capacity_gate`` correctly sees four separate
    owners and lets all four run at once.
    """
    if left == right:
        pytest.skip("a submission is not its own near-twin")
    match = same_budget(_production(left), _production(right))
    assert match is not None, f"{left} and {right} should share a budget"


@pytest.mark.parametrize("left", _INDEPENDENT)
@pytest.mark.parametrize("right", _INDEPENDENT)
def test_independent_miners_keep_separate_budgets(left: str, right: str) -> None:
    """Sharing the starter kit is not sharing a budget.

    The stakes are asymmetric and this is the expensive direction: a threshold
    that merged two genuinely independent miners would silently halve an honest
    miner's throughput, which is worse than having no gate at all.
    """
    if left == right:
        return
    assert same_budget(_production(left), _production(right)) is None


@pytest.mark.parametrize("family", _FAMILY)
@pytest.mark.parametrize("miner", _INDEPENDENT)
def test_the_family_is_not_grouped_with_independent_miners(
    family: str, miner: str
) -> None:
    assert same_budget(_production(family), _production(miner)) is None


def test_measured_separation_leaves_room_on_both_sides() -> None:
    """The defaults sit in a gap, not on the edge of one.

    Pins the actual production margin so a future retune has to confront it:
    the weakest within-family pair still clears a threshold, and the strongest
    independent-miner pair misses *both* by a wide margin.
    """
    family = [_production(name) for name in _FAMILY]
    independent = [_production(name) for name in _INDEPENDENT]

    def strongest(sketches: list[SubmissionSketch]) -> list[tuple[float, float]]:
        return [
            similarity_scores(left, right)
            for index, left in enumerate(sketches)
            for right in sketches[index + 1 :]
        ]

    weakest_family = min(max(pair) for pair in strongest(family))
    strongest_independent = max(max(pair) for pair in strongest(independent))

    assert weakest_family >= DEFAULT_CONTAINMENT_THRESHOLD
    assert strongest_independent < DEFAULT_JACCARD_THRESHOLD
    assert strongest_independent < DEFAULT_CONTAINMENT_THRESHOLD
    # A margin, not a coincidence: the two populations are far apart.
    assert weakest_family - strongest_independent > 0.4


def test_the_family_collapses_to_one_budget() -> None:
    """The four family submissions plus four miners resolve to five budgets."""
    sketches = [_production(name) for name in (*_FAMILY, *_INDEPENDENT)]
    budgets = similarity_budgets(sketches)

    assert len(budgets) == 5
    assert len(budgets[0]) == 4
    assert set(budgets[0]) == {_production(name).agent_id for name in _FAMILY}


# ---------------------------------------------------------------------------
# The rule's guards -- synthetic
# ---------------------------------------------------------------------------


def test_identical_sketches_under_different_ids_group() -> None:
    shared = _hashes(0, 256)
    left = _synthetic(*shared, card=4000)
    right = _synthetic(*shared, card=4000)

    match = same_budget(left, right)

    assert match is not None
    assert match.jaccard == 1.0
    assert match.channel == "jaccard"
    assert str(right.agent_id) in match.describe()


def test_a_submission_is_never_its_own_near_twin() -> None:
    sketch = _synthetic(*_hashes(0, 256), card=4000)
    assert same_budget(sketch, sketch) is None


def test_missing_evidence_never_groups() -> None:
    """A submission the platform cannot compare keeps its own budget."""
    present = _synthetic(*_hashes(0, 256), card=4000)
    absent = SubmissionSketch(agent_id=uuid4(), fingerprint=None)
    empty = SubmissionSketch(
        agent_id=uuid4(),
        fingerprint={"v": 2, "k": 256, "card": 0, "m": [], "corpus": "c0ffee"},
    )

    assert same_budget(present, absent) is None
    assert same_budget(present, empty) is None
    assert same_budget(absent, empty) is None


def test_a_different_reference_corpus_never_groups() -> None:
    """Two corpus generations are not comparable, so they are not grouped.

    The estimator refuses the comparison; the rule inherits the refusal rather
    than papering over it, which is the safe direction for a fairness gate.
    """
    shared = _hashes(0, 256)
    left = _synthetic(*shared, card=4000)
    right = _synthetic(*shared, card=4000)
    assert right.fingerprint is not None
    other_corpus = SubmissionSketch(
        agent_id=right.agent_id,
        fingerprint={**right.fingerprint, "corpus": "deadbeef"},
    )

    assert same_budget(left, right) is not None
    assert same_budget(left, other_corpus) is None


def test_tiny_residuals_are_not_evidence() -> None:
    """Below the shingle floor, two miners can coincide by pasting one fix."""
    shared = _hashes(0, 8)
    left = _synthetic(*shared, card=8)
    right = _synthetic(*shared, card=8)

    assert same_budget(left, right) is None
    relaxed = SimilarityBudgetPolicy(min_shingles=4)
    assert same_budget(left, right, policy=relaxed) is not None


def test_the_scale_guard_blocks_small_inside_large() -> None:
    """Containment alone would call a small residual inside a big one a twin.

    ``|A n B| / min(|A|, |B|)`` is 1.0 whenever the smaller set is a subset,
    however unlike the two crates are. The size-ratio guard is what makes the
    containment channel safe to read at all.
    """
    small = _synthetic(*_hashes(0, 100), card=100)
    large = _synthetic(*_hashes(0, 256), card=100_000)

    assert same_budget(small, large) is None
    # Without the guard the containment channel would have fired outright.
    assert similarity_scores(small, large)[1] >= DEFAULT_CONTAINMENT_THRESHOLD


def test_the_containment_channel_fires_on_a_padded_resubmission() -> None:
    """A copy that bolts on extra files dilutes Jaccard but not containment."""
    core = _hashes(0, 200)
    original = _synthetic(*core, card=1000)
    padded = _synthetic(*core, *_hashes(10_000, 56), card=1500)

    match = same_budget(original, padded)

    assert match is not None
    assert match.channel == "containment"
    assert match.jaccard < DEFAULT_JACCARD_THRESHOLD


def test_thresholds_are_operator_configurable() -> None:
    """The same pair groups or does not, purely as a function of policy."""
    left = _synthetic(*_hashes(0, 200), *_hashes(1000, 56), card=1000)
    right = _synthetic(*_hashes(0, 200), *_hashes(2000, 56), card=1000)

    strict = SimilarityBudgetPolicy(jaccard_threshold=0.99, containment_threshold=0.99)
    loose = SimilarityBudgetPolicy(jaccard_threshold=0.70, containment_threshold=0.70)

    assert same_budget(left, right, policy=strict) is None
    assert same_budget(left, right, policy=loose) is not None


@pytest.mark.parametrize("threshold", [0.0, 0.5, 0.69, 1.01])
def test_a_threshold_outside_the_bounds_is_refused(threshold: float) -> None:
    """An operator cannot configure the false grouping this rule avoids."""
    with pytest.raises(ValueError, match="0.625"):
        SimilarityBudgetPolicy(jaccard_threshold=threshold)
    with pytest.raises(ValueError, match="0.625"):
        SimilarityBudgetPolicy(containment_threshold=threshold)


def test_default_policy_matches_the_shipped_constants() -> None:
    policy = SimilarityBudgetPolicy()
    assert policy.jaccard_threshold == DEFAULT_JACCARD_THRESHOLD
    assert policy.containment_threshold == DEFAULT_CONTAINMENT_THRESHOLD
    assert policy.min_shingles == MIN_COMPARABLE_SHINGLES


# ---------------------------------------------------------------------------
# The two collection helpers
# ---------------------------------------------------------------------------


def test_similar_submissions_orders_strongest_first() -> None:
    candidate = _synthetic(*_hashes(0, 200), card=4000)
    exact = _synthetic(*_hashes(0, 200), card=4000)
    padded = _synthetic(*_hashes(0, 200), *_hashes(9000, 56), card=4200)
    unrelated = _synthetic(*_hashes(50_000, 200), card=4000)

    matches = similar_submissions(candidate, [unrelated, padded, exact])

    assert [match.agent_id for match in matches] == [exact.agent_id, padded.agent_id]
    assert matches[0].jaccard >= matches[1].jaccard


def test_similar_submissions_does_not_chain() -> None:
    """The enforcement path compares only against the candidate.

    A and C are unrelated; B is close to both. Single linkage would put all
    three in one budget, which on the enforcement path would let two bridge
    submissions charge an unrelated third for capacity it never shared.
    """
    # Three overlapping windows: A~B and B~C clear 0.70, A~C does not.
    a = _synthetic(*_hashes(0, 256), card=256)
    b = _synthetic(*_hashes(45, 256), card=256)
    c = _synthetic(*_hashes(90, 256), card=256)

    permissive = SimilarityBudgetPolicy(
        jaccard_threshold=0.70, containment_threshold=0.70
    )
    assert same_budget(a, b, policy=permissive) is not None
    assert same_budget(b, c, policy=permissive) is not None

    assert same_budget(a, c, policy=permissive) is None
    assert similar_submissions(a, [c], policy=permissive) == ()
    assert len(similarity_budgets([a, b, c], policy=permissive)) == 1


def test_similarity_budgets_keeps_singletons_and_orders_largest_first() -> None:
    shared = _hashes(0, 256)
    twins = [_synthetic(*shared, card=4000) for _ in range(3)]
    loner = _synthetic(*_hashes(90_000, 256), card=4000)

    budgets = similarity_budgets([loner, *twins])

    assert len(budgets) == 2
    assert len(budgets[0]) == 3
    assert budgets[1] == (loner.agent_id,)


def test_similarity_budgets_of_nothing_is_nothing() -> None:
    assert similarity_budgets([]) == ()
