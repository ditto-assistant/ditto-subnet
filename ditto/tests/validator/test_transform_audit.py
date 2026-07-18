"""Cross-repo determinism vectors for the transform audit derivation.

The vectors below were emitted by the Go implementation in dittobench-datagen
``persona/transform.go``. If this file fails, the validator and the generator
disagree about which cases are audited, and no verdict either one computes is
reproducible by the other. Mirrors the pairing that
``test_onchain_seed.py`` pins for the dataset seed.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ditto.api_models.validator import ScoreReport
from ditto.validator.transform_audit import (
    AUDIT_MIN_PAIRS,
    AUDIT_MIN_ROBUSTNESS,
    audit_selected,
    audit_transform_id,
    brittleness_signature,
    transform_robustness,
)
from ditto.validator.worker import _attach_transform_audit

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


def test_transform_robustness_reads_details() -> None:
    value, pairs = transform_robustness(
        {"transform_robustness": 0.5, "audit_case_count": 6}
    )
    assert value == 0.5
    assert pairs == 6


@pytest.mark.parametrize("details", [None, {}, {"audit_case_count": 6}])
def test_absent_metric_is_not_a_failure(details) -> None:
    """An older engine that reports nothing must not read as a failed audit."""
    assert transform_robustness(details) == (None, 0)
    assert brittleness_signature([details]) is False


def test_brittleness_signature_uses_the_median() -> None:
    """One low run is noise; the median across finalized reports is the signal."""
    low = {"transform_robustness": 0.1, "audit_case_count": AUDIT_MIN_PAIRS}
    high = {"transform_robustness": 1.0, "audit_case_count": AUDIT_MIN_PAIRS}
    # A single low run among three does not trip the verdict.
    assert brittleness_signature([low, high, high]) is False
    # A majority does.
    assert brittleness_signature([low, low, high]) is True


def test_thin_evidence_is_not_judged() -> None:
    """Too few audit pairs behind a value: a single split would swing the rate."""
    thin = {
        "transform_robustness": 0.0,
        "audit_case_count": AUDIT_MIN_PAIRS - 1,
    }
    assert brittleness_signature([thin, thin, thin]) is False


def test_honest_robustness_clears_the_floor() -> None:
    """The floor must sit below what a consistent harness scores."""
    honest = {"transform_robustness": 1.0, "audit_case_count": 8}
    assert brittleness_signature([honest, honest, honest]) is False
    assert AUDIT_MIN_ROBUSTNESS < 1.0


def _report(robustness: float | None, pairs: int = AUDIT_MIN_PAIRS) -> ScoreReport:
    """A real ScoreReport so the helper is exercised against the wire model it
    actually receives, not a stand-in that could drift from it."""
    details: dict | None = None
    if robustness is not None:
        details = {"transform_robustness": robustness, "audit_case_count": pairs}
    return ScoreReport(
        run_id="run-1",
        seed=1,
        composite=0.5,
        tool_mean=0.5,
        memory_mean=0.5,
        median_ms=10,
        n=10,
        generated_at=datetime(2026, 7, 18, tzinfo=UTC),
        per_case=[],
        structural_fingerprint=None,
        details=details,
    )


def test_attach_transform_audit_records_the_median() -> None:
    """The platform sees only the representative report, so the validator must
    attach the median verdict over the K confirmation runs itself."""
    out = _attach_transform_audit(
        _report(0.2), [_report(0.1), _report(0.2), _report(0.9)]
    )
    assert out.details is not None
    assert out.details["transform_audit_failed"] is True
    assert out.details["transform_robustness_median"] == 0.2
    assert out.details["transform_audit_runs"] == 3

    out2 = _attach_transform_audit(
        _report(1.0), [_report(1.0), _report(1.0), _report(0.2)]
    )
    assert out2.details is not None
    assert out2.details["transform_audit_failed"] is False


def test_attach_transform_audit_noop_without_metric() -> None:
    """An older scoring engine reports nothing; leave the report untouched
    rather than recording a verdict that was never measured."""
    rep = _report(None)
    out = _attach_transform_audit(rep, [_report(None)])
    assert out is rep
    assert out.details is None
