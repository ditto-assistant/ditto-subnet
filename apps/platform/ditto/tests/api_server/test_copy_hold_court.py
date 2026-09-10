"""Pure-rule classification and verdict tests for the copy-hold triage court."""

from __future__ import annotations

from ditto.api_models.copy_court_settings import CopyCourtSettings
from ditto.api_server.copy_court_rules import (
    HOLD_CLASS_BYTE_IDENTICAL,
    HOLD_CLASS_CROSS_MINER,
    HOLD_CLASS_NEAR_DUPLICATE,
    HOLD_CLASS_REPACK,
    HOLD_CLASS_UNKNOWN,
    AncestorIdentity,
    CandidateIdentity,
    byte_identical_verdict,
    classify_hold,
    repack_verdict,
)

_CAND = CandidateIdentity(
    agent_id="aaaa",
    miner_hotkey="5Held",
    sha256="ab" * 32,
    normalized_source_hash="cd" * 32,
)

_ANCESTOR = AncestorIdentity(
    agent_id="bbbb",
    name="unione",
    version=21,
    miner_hotkey="5Held",
    sha256="ab" * 32,
    normalized_source_hash="cd" * 32,
    reject_reason="Reject unione v21: limb b.",
    resolved_at="2026-09-01T13:00:00+00:00",
)

_BYTE_IDENTICAL_REASON = (
    "Same miner, previously rejected as unione v21 (agent bbbb, uploaded "
    "2026-09-01T12:38:29.010909+00:00). This upload is byte-identical to that "
    "rejected artifact. Held for operator review to check whether the cited "
    "behavior was removed."
)

_CANONICALIZED_REASON = (
    "Same miner, previously rejected as unione v21 (agent bbbb, uploaded "
    "2026-09-01T12:38:29.010909+00:00). This upload is the same canonicalized "
    "source as that rejected artifact. Held for operator review to check "
    "whether the cited behavior was removed."
)

_CROSS_MINER_REASON = (
    "Resubmission of a rejected artifact: this upload is byte-identical to "
    "unione v21 (agent bbbb, hotkey 5Other), which an operator rejected "
    "before this artifact was uploaded. Held for operator review."
)

_NEAR_DUPLICATE_REASON = (
    "content near-duplicate of agent bbbb: composite delta 0.0255, jaccard "
    "0.969, containment 0.977; structural jaccard 0.965, containment 0.992"
)

_SIZE_FALLBACK_REASON = "near-duplicate of agent bbbb: size-cluster match at 500k bytes"


def test_byte_identical_same_miner_classifies_and_rejects() -> None:
    hold_class = classify_hold(_BYTE_IDENTICAL_REASON, _CAND, _ANCESTOR)
    assert hold_class == HOLD_CLASS_BYTE_IDENTICAL
    verdict = byte_identical_verdict(_CAND, _ANCESTOR)
    assert verdict.verdict == "reject"
    assert "false by construction" in verdict.reason
    reject_reason = _ANCESTOR.reject_reason or ""
    assert reject_reason in verdict.reason
    assert verdict.citations[0]["agent_id"] == "bbbb"
    assert verdict.evidence["identity_basis"] == "exact_sha256"


def test_canonicalized_identity_classifies_as_repack() -> None:
    candidate = CandidateIdentity(
        agent_id="aaaa",
        miner_hotkey="5Held",
        sha256="ef" * 32,
        normalized_source_hash="cd" * 32,
    )
    assert (
        classify_hold(_CANONICALIZED_REASON, candidate, _ANCESTOR) == HOLD_CLASS_REPACK
    )
    verdict = repack_verdict(candidate, _ANCESTOR)
    assert verdict.verdict == "reject"
    assert verdict.evidence["identity_basis"] == "normalized_source_hash"


def test_identity_contradicting_the_hint_is_unknown() -> None:
    candidate = CandidateIdentity(
        agent_id="aaaa",
        miner_hotkey="5Held",
        sha256="99" * 32,
        normalized_source_hash="88" * 32,
    )
    assert classify_hold(_BYTE_IDENTICAL_REASON, candidate, _ANCESTOR) == (
        HOLD_CLASS_UNKNOWN
    )


def test_cross_miner_resubmission_is_its_own_class() -> None:
    ancestor = AncestorIdentity(
        agent_id="bbbb",
        name="unione",
        version=21,
        miner_hotkey="5Other",
        sha256="ab" * 32,
        normalized_source_hash="cd" * 32,
        reject_reason="Reject",
        resolved_at=None,
    )
    assert classify_hold(_CROSS_MINER_REASON, _CAND, ancestor) == HOLD_CLASS_CROSS_MINER


def test_near_duplicate_and_size_fallback_classify() -> None:
    assert (
        classify_hold(_NEAR_DUPLICATE_REASON, _CAND, _ANCESTOR)
        == HOLD_CLASS_NEAR_DUPLICATE
    )
    assert (
        classify_hold(_SIZE_FALLBACK_REASON, _CAND, _ANCESTOR)
        == HOLD_CLASS_NEAR_DUPLICATE
    )


def test_unknown_reason_and_missing_ancestor_stay_unknown() -> None:
    assert classify_hold("something unrecognizable", _CAND, _ANCESTOR) == (
        HOLD_CLASS_UNKNOWN
    )
    assert classify_hold(_BYTE_IDENTICAL_REASON, _CAND, None) == HOLD_CLASS_UNKNOWN


def test_effective_mode_is_master_capped_per_class() -> None:
    settings = CopyCourtSettings(
        mode="shadow",
        byte_identical_resubmission_mode="enforce",
    )
    assert settings.effective_mode("rejected_resubmission_byte_identical") == ("shadow")
    assert settings.effective_mode("near_duplicate") == "off"
    enforced = CopyCourtSettings(
        mode="enforce",
        byte_identical_resubmission_mode="enforce",
    )
    assert enforced.effective_mode("rejected_resubmission_byte_identical") == (
        "enforce"
    )
    assert enforced.effective_mode("near_duplicate") == "off"
