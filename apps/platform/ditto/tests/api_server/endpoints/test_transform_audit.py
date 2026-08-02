"""Reproduce-under-transform audit verdict at score finalization (v3 Part A4)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ditto.api_server.endpoints.validator import (
    AUDIT_ALPHA,
    AUDIT_MIN_DISCORDANT,
    _binomial_tail,
    _transform_audit_verdict,
)


def score(
    both_correct=0,
    base_only=0,
    transform_only=0,
    both_wrong=0,
    key="audit_pairs_pooled",
):
    return SimpleNamespace(
        details={
            key: {
                "both_correct": both_correct,
                "base_only": base_only,
                "transform_only": transform_only,
                "both_wrong": both_wrong,
            }
        }
    )


def test_honest_symmetric_splits_are_not_brittleness() -> None:
    """The measured honest model splits pairs BOTH ways (5 vs 6). If this ever
    flags, the audit is punishing model noise rather than brittleness."""
    pvalue, pooled, failed = _transform_audit_verdict(
        [score(base_only=5, transform_only=6)]
    )
    assert failed is False
    assert pvalue is not None and pvalue > 0.5
    assert pooled["base_only"] == 5


def test_directional_splits_are_brittleness() -> None:
    """The measured brittle harness: discordant pairs all one direction."""
    pvalue, _, failed = _transform_audit_verdict([score(base_only=7)])
    assert failed is True
    assert pvalue is not None and pvalue <= AUDIT_ALPHA


def test_counts_pool_across_validators() -> None:
    """Each validator pools its own runs; the platform pools across the k=3, so
    no single validator's handful of pairs decides an agent's fate."""
    scores = [score(base_only=3), score(base_only=3), score(base_only=2)]
    pvalue, pooled, failed = _transform_audit_verdict(scores)
    assert pooled["base_only"] == 8
    assert failed is True
    # The same eight events split both ways is not a signature.
    _, _, failed2 = _transform_audit_verdict([score(base_only=4, transform_only=4)])
    assert failed2 is False


def test_thin_evidence_is_never_a_verdict() -> None:
    """Below MIN_DISCORDANT the exact test cannot reach ALPHA, so no verdict."""
    pvalue, _, failed = _transform_audit_verdict(
        [score(base_only=AUDIT_MIN_DISCORDANT - 1)]
    )
    assert failed is False
    assert pvalue is None


def test_both_wrong_pairs_do_not_drive_the_verdict() -> None:
    """Both-wrong was 81% of pairs in calibration and reflects accuracy, which
    the composite already scores."""
    _, _, failed = _transform_audit_verdict([score(both_wrong=500)])
    assert failed is False
    _, _, failed2 = _transform_audit_verdict([score(both_correct=500)])
    assert failed2 is False


def test_absent_counts_are_not_a_failed_audit() -> None:
    """An older scoring engine reports nothing. Holding an agent on that would
    quarantine a miner for the platform's own upgrade lag."""
    pvalue, pooled, failed = _transform_audit_verdict(
        [SimpleNamespace(details={}), SimpleNamespace(details=None)]
    )
    assert (pvalue, failed) == (None, False)
    assert sum(pooled.values()) == 0


def test_reads_per_run_counts_when_validator_did_not_pool() -> None:
    """A validator that emitted raw per-run counts still contributes."""
    _, pooled, _ = _transform_audit_verdict([score(base_only=6, key="audit_pairs")])
    assert pooled["base_only"] == 6


def test_alpha_is_the_honest_false_positive_rate() -> None:
    """The property that makes acting on this defensible at all."""
    assert 0 < AUDIT_ALPHA <= 0.01
    assert _binomial_tail(AUDIT_MIN_DISCORDANT, AUDIT_MIN_DISCORDANT) <= 0.02
    assert _binomial_tail(0, 10) == pytest.approx(1.0)


def test_enforcement_is_off_by_default() -> None:
    """The metric now discriminates in principle, but the floor has not been
    validated against the champion population it judges. Flipping this without
    that evidence is what would cost miners."""
    from ditto.api_server.endpoints import validator as v

    assert v.TRANSFORM_AUDIT_ENFORCE is False
