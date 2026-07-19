"""Cross-repo determinism vectors for the transform audit derivation.

The vectors below were emitted by the Go implementation in dittobench-datagen
``persona/transform.go``. If this file fails, the validator and the generator
disagree about which cases are audited, and no verdict either one computes is
reproducible by the other. Mirrors the pairing that
``test_onchain_seed.py`` pins for the dataset seed.
"""

from __future__ import annotations

import pytest

from ditto.validator.transform_audit import (
    ALPHA,
    MIN_DISCORDANT,
    audit_selected,
    audit_transform_id,
    binomial_tail,
    brittleness_pvalue,
    brittleness_signature,
    pool_audit_pairs,
)

# (seed, case_id, selected, transform_id) straight from the Go implementation.
GO_VECTORS = [
    (0, "q-rec-city", False, 2785),
    (0, "q-ku-occupation", False, 3664),
    (0, "q-pit-employer", False, 321),
    (0, "q-inv-a-partner", False, 2814),
    (1, "q-rec-city", False, 1653),
    (1, "q-ku-occupation", True, 608),
    (1, "q-pit-employer", True, 3814),
    (1, "q-inv-a-partner", False, 4068),
    (42, "q-rec-city", True, 2675),
    (42, "q-ku-occupation", False, 3823),
    (42, "q-pit-employer", False, 955),
    (42, "q-inv-a-partner", False, 1203),
    (123456789, "q-rec-city", False, 1086),
    (123456789, "q-ku-occupation", False, 3507),
    (123456789, "q-pit-employer", False, 398),
    (123456789, "q-inv-a-partner", False, 2781),
    (9223372036854775807, "q-rec-city", True, 2546),
    (9223372036854775807, "q-ku-occupation", False, 3101),
    (9223372036854775807, "q-pit-employer", False, 1141),
    (9223372036854775807, "q-inv-a-partner", False, 1662),
]


@pytest.mark.parametrize("seed,case_id,selected,transform_id", GO_VECTORS)
def test_matches_go_implementation(
    seed: int, case_id: str, selected: bool, transform_id: int
) -> None:
    assert audit_selected(seed, case_id) is selected
    assert audit_transform_id(seed, case_id) == transform_id


def test_transform_id_in_range() -> None:
    for seed in range(200):
        assert 0 <= audit_transform_id(seed, "q-rec-city") < 4096


def test_selection_rate_is_near_the_public_rate() -> None:
    """The draw must actually land near AUDIT_BPS, not merely be deterministic."""
    hits = sum(audit_selected(1234, f"q-rec-{i}") for i in range(10000))
    assert 1300 <= hits <= 1700, f"selection rate {hits / 10000:.4f} is far from 15%"


def pairs(both_correct=0, base_only=0, transform_only=0, both_wrong=0):
    return {
        "audit_pairs": {
            "both_correct": both_correct,
            "base_only": base_only,
            "transform_only": transform_only,
            "both_wrong": both_wrong,
        }
    }


def test_binomial_tail_matches_known_values() -> None:
    assert binomial_tail(0, 0) == 1.0
    assert binomial_tail(6, 6) == pytest.approx(0.5**6)
    assert binomial_tail(0, 6) == pytest.approx(1.0)
    assert binomial_tail(3, 6) == pytest.approx(0.65625)


def test_honest_symmetric_splits_are_not_brittleness() -> None:
    """The measured honest model: 5 base-only vs 6 transform-only.

    A nondeterministic model splits pairs in BOTH directions, which is the null
    this test is built around. If this ever starts flagging, the audit is
    punishing model noise.
    """
    assert brittleness_pvalue(5, 6) > 0.5
    assert brittleness_signature([pairs(base_only=5, transform_only=6)]) is False


def test_directional_splits_are_brittleness() -> None:
    """The measured brittle harness: discordant pairs all one way."""
    assert brittleness_pvalue(7, 0) <= ALPHA
    assert brittleness_signature([pairs(base_only=7)]) is True


def test_thin_evidence_is_never_a_verdict() -> None:
    """Too few discordant pairs cannot reach ALPHA, so no verdict is attempted."""
    assert brittleness_signature([pairs(base_only=MIN_DISCORDANT - 1)]) is False
    # Even a perfect run of one-directional pairs below the floor stays silent.
    assert brittleness_signature([pairs(base_only=5)]) is False


def test_both_wrong_pairs_do_not_drive_the_verdict() -> None:
    """Both-wrong is the large majority on a hard benchmark (81% measured) and
    reflects accuracy, which the composite already scores."""
    assert brittleness_signature([pairs(both_wrong=500)]) is False
    assert brittleness_signature([pairs(both_correct=500)]) is False


def test_counts_pool_across_runs() -> None:
    """A single run yields too few pairs to decide; the evidence accumulates."""
    runs = [pairs(base_only=3), pairs(base_only=3), pairs(base_only=2)]
    pooled = pool_audit_pairs(runs)
    assert pooled["base_only"] == 8
    assert brittleness_signature(runs) is True
    # The same eight events split across directions is not a signature.
    assert brittleness_signature([pairs(base_only=4, transform_only=4)]) is False


@pytest.mark.parametrize("details", [None, {}, {"audit_pairs": "nope"}])
def test_absent_counts_are_not_a_failure(details) -> None:
    """An older engine reporting nothing must not read as a failed audit."""
    assert brittleness_signature([details]) is False


def test_alpha_is_the_honest_false_positive_rate() -> None:
    """The property that makes this defensible: under the null, the chance of
    flagging an honest harness is at most ALPHA."""
    assert 0 < ALPHA <= 0.01
    # A fair coin producing MIN_DISCORDANT all-one-way results is rare enough.
    assert binomial_tail(MIN_DISCORDANT, MIN_DISCORDANT) <= 0.02
