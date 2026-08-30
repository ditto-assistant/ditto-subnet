import pytest

from ditto_screening_protocol.coding_source_screen import (
    CodingSourceScreenEvidence,
    CodingSourceScreenFinding,
    CodingSourceScreenOutcome,
    CodingSourceScreenSeverity,
    coding_source_screen_digest,
)


def _evidence(
    outcome: CodingSourceScreenOutcome, findings: list[CodingSourceScreenFinding]
) -> CodingSourceScreenEvidence:
    raw = {
        "schema": "dittobench-coding-source-screen-v1",
        "coding_contract_version": 1,
        "weight_eligible": False,
        "agent_artifact_sha256": "a" * 64,
        "screened_image_sha256": "b" * 64,
        "analyzer_version": "coding-source-v1",
        "policy_version": 1,
        "outcome": outcome,
        "findings": tuple(findings),
        "evidence_sha256": "0" * 64,
    }
    provisional = CodingSourceScreenEvidence.model_construct(**raw)
    raw["evidence_sha256"] = coding_source_screen_digest(provisional)
    return CodingSourceScreenEvidence.model_validate(raw)


def test_deny_evidence_is_content_addressed() -> None:
    finding = CodingSourceScreenFinding(
        rule_id="docker-socket",
        severity=CodingSourceScreenSeverity.DENY,
        evidence_sha256="c" * 64,
    )
    evidence = _evidence(CodingSourceScreenOutcome.DENY, [finding])
    assert evidence.weight_eligible is False
    assert evidence.evidence_sha256 == coding_source_screen_digest(evidence)


def test_outcome_rules_fail_closed() -> None:
    advisory = CodingSourceScreenFinding(
        rule_id="dead-code",
        severity=CodingSourceScreenSeverity.ADVISORY,
        evidence_sha256="c" * 64,
    )
    with pytest.raises(ValueError):
        _evidence(CodingSourceScreenOutcome.DENY, [advisory])
    with pytest.raises(ValueError):
        _evidence(CodingSourceScreenOutcome.PASS, [advisory])
    deny = CodingSourceScreenFinding(
        rule_id="docker-socket",
        severity=CodingSourceScreenSeverity.DENY,
        evidence_sha256="c" * 64,
    )
    with pytest.raises(ValueError, match="advisory"):
        _evidence(CodingSourceScreenOutcome.ADVISORY, [deny])
    with pytest.raises(ValueError, match="quarantine"):
        _evidence(CodingSourceScreenOutcome.QUARANTINE, [deny])
