"""Parity tests for :mod:`ditto.bench.runner.antigaming`.

Each test mirrors a case in ``go/bittensor/antigaming_test.go`` so the
Python port stays byte-identical with the canonical Go anti-gaming helpers.
"""

from __future__ import annotations

import pytest

from ditto.bench.runner.antigaming import (
    CanaryIdenticalError,
    HiddenSet,
    distractor_bundle_for,
    ensure_paraphrase_changed,
    memorisation_discount,
    normalise_prompt_for_canary_check,
    paraphrase_seed,
    partition_fixture,
)


def _sorted_set(values: list[str]) -> list[str]:
    return sorted(values)


def test_partition_fixture_deterministic_and_rotates() -> None:
    """Same secret -> same partition; rotating the secret rotates buckets."""
    ids = ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j"]
    first = partition_fixture(ids, "secret-1", private_frac=0.3, canary_frac=0.2)
    again = partition_fixture(ids, "secret-1", private_frac=0.3, canary_frac=0.2)
    assert _sorted_set(first.private) == _sorted_set(again.private)
    assert _sorted_set(first.canary) == _sorted_set(again.canary)
    assert _sorted_set(first.public) == _sorted_set(again.public)

    rotated = partition_fixture(ids, "secret-2", private_frac=0.3, canary_frac=0.2)
    assert not (
        _sorted_set(first.private) == _sorted_set(rotated.private)
        and _sorted_set(first.canary) == _sorted_set(rotated.canary)
    )

    total = len(first.public) + len(first.private) + len(first.canary)
    assert total == len(ids), "partition lost or duplicated case IDs"


def test_partition_fixture_clamps_to_public_minimum() -> None:
    """Even pathological fractions leave at least one public case."""
    ids = ["a", "b", "c", "d"]
    split = partition_fixture(ids, "s", private_frac=0.6, canary_frac=0.6)
    assert len(split.public) >= 1, split


def test_partition_fixture_returns_hidden_set() -> None:
    """Return type is the documented :class:`HiddenSet` dataclass."""
    split = partition_fixture(["a"], "s", private_frac=0.0, canary_frac=0.0)
    assert isinstance(split, HiddenSet)
    assert split.public == ["a"]
    assert split.private == [] and split.canary == []


def test_paraphrase_seed_deterministic_and_rotates() -> None:
    """The seed is deterministic in (secret, case_id) and 64-hex chars long."""
    a = paraphrase_seed("secret-1", "case-x")
    b = paraphrase_seed("secret-1", "case-x")
    assert a == b
    assert paraphrase_seed("secret-2", "case-x") != a
    assert paraphrase_seed("secret-1", "case-y") != a
    assert len(a) == 64 and all(ch in "0123456789abcdef" for ch in a)


def test_memorisation_discount_curve() -> None:
    """Discount matches the Go reference at every documented gap level."""
    assert (
        memorisation_discount(
            0.9, 0.4, 0, gap_threshold=0.1, gap_ceiling=0.5, max_discount=0.5
        )
        == 1.0
    )
    assert (
        memorisation_discount(
            0.9, 0.85, 10, gap_threshold=0.1, gap_ceiling=0.5, max_discount=0.5
        )
        == 1.0
    )
    mid = memorisation_discount(
        0.9, 0.6, 10, gap_threshold=0.1, gap_ceiling=0.5, max_discount=0.5
    )
    assert 0.74 < mid < 0.76, f"expected ~0.75, got {mid}"
    saturated = memorisation_discount(
        0.9, 0.0, 10, gap_threshold=0.1, gap_ceiling=0.5, max_discount=0.5
    )
    assert saturated == 0.5


def test_distractor_bundle_avoids_expected_and_forbidden() -> None:
    """Distractor pool never overlaps the expected or forbidden IDs."""
    candidates = ["good1", "bad1", "x1", "x2", "x3", "x4", "x5"]
    got = distractor_bundle_for(
        "ctest",
        ["good1"],
        ["bad1"],
        candidates,
        "secret",
        3,
    )
    assert len(got) == 3
    assert "good1" not in got and "bad1" not in got

    again = distractor_bundle_for("ctest", ["good1"], ["bad1"], candidates, "secret", 3)
    assert _sorted_set(again) == _sorted_set(got)

    rotated = distractor_bundle_for(
        "ctest", ["good1"], ["bad1"], candidates, "different-secret", 3
    )
    assert _sorted_set(rotated) != _sorted_set(got)


def test_distractor_bundle_empty_inputs() -> None:
    """Empty candidate pool or non-positive n returns an empty list."""
    assert distractor_bundle_for("c", [], [], [], "s", 5) == []
    assert distractor_bundle_for("c", [], [], ["a", "b"], "s", 0) == []


def test_ensure_paraphrase_changed_identical_raises() -> None:
    """Identical strings (or punctuation-only diffs) are rejected."""
    with pytest.raises(CanaryIdenticalError):
        ensure_paraphrase_changed("What did I say?", "What did I say?")
    with pytest.raises(CanaryIdenticalError):
        ensure_paraphrase_changed("What did I say?", "What did I say???")


def test_ensure_paraphrase_changed_real_paraphrase_passes() -> None:
    """A real reword passes the canary-identity check."""
    ensure_paraphrase_changed("What did I say?", "What was it I told you?")


def test_normalise_prompt_for_canary_check_collapses_whitespace() -> None:
    """Normalisation lowercases, drops punctuation, and collapses spaces."""
    assert normalise_prompt_for_canary_check("  Hello,  WORLD!!!  ") == "hello world"


def test_aggregate_with_discount_penalises_memorising_miner() -> None:
    """Mirror of ``TestAggregateWithDiscount_PenalisesMemorisingMiner`` in Go.

    Two miners share an identical public mean but only miner A's canary
    mean tracks it. Miner B (canary collapsed) must end up with strictly
    lower normalised weight.
    """
    from ditto.bench.loader.taxonomy import Mechanism
    from ditto.bench.runner.scoring import Score, aggregate_with_discount

    def _score(cid: str, visibility: str, value: float) -> Score:
        return Score(
            schema_version="dittobench/1",
            case_id=cid,
            mechanism=Mechanism.CORE,
            score=value,
            challenge_id=cid,
            visibility=visibility,
        )

    scores = [
        _score("a1", "public", 0.90),
        _score("a2", "public", 0.90),
        _score("a3", "canary", 0.88),
        _score("a4", "canary", 0.89),
        _score("b1", "public", 0.90),
        _score("b2", "public", 0.90),
        _score("b3", "canary", 0.30),
        _score("b4", "canary", 0.30),
    ]
    hk = {
        "a1": "A",
        "a2": "A",
        "a3": "A",
        "a4": "A",
        "b1": "B",
        "b2": "B",
        "b3": "B",
        "b4": "B",
    }
    weights, details = aggregate_with_discount(scores, hk, Mechanism.CORE)
    assert set(weights) == {"A", "B"}
    assert abs(sum(weights.values()) - 1.0) < 1e-6
    assert weights["A"] > weights["B"], (
        f"memorising miner B should be penalised: {weights}"
    )
    by_hk = {d.hotkey: d for d in details}
    assert by_hk["A"].discount == 1.0
    assert by_hk["B"].discount < 1.0


def test_aggregate_with_discount_ignores_unmapped_challenges() -> None:
    """Scores whose challenge_id is absent from the map are dropped silently."""
    from ditto.bench.loader.taxonomy import Mechanism
    from ditto.bench.runner.scoring import Score, aggregate_with_discount

    scores = [
        Score(
            schema_version="dittobench/1",
            case_id="x",
            mechanism=Mechanism.CORE,
            score=0.5,
            challenge_id="x",
            visibility="public",
        ),
        Score(
            schema_version="dittobench/1",
            case_id="y",
            mechanism=Mechanism.CORE,
            score=0.7,
            challenge_id="y",
            visibility="public",
        ),
    ]
    weights, _ = aggregate_with_discount(scores, {"x": "A"}, Mechanism.CORE)
    assert weights == {"A": 1.0}
