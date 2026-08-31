"""Bounds and digest-binding for the quarantine review wire payloads."""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from ditto_screening_protocol import (
    SCREENING_POLICY_VERSION,
    ScreenEvidenceItem,
    ScreenResultOutcome,
    ScreenResultRequest,
    SourceReviewFinding,
    SourceReviewNote,
    source_review_notes_digest,
)

_HOTKEY = "5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm"


def _finding() -> SourceReviewFinding:
    return SourceReviewFinding(
        artifact_sha256="de" * 32,
        prompt_revision="source-review-v2",
        risk_level="medium",
        confidence=0.8,
        categories=["suspicious_static_tables"],
        evidence=[
            {"path": "src/table.rs", "line": 3, "category": "suspicious_static_tables"}
        ],
        summary="Large static answer table shapes the response path.",
    )


def _request(**overrides: object) -> ScreenResultRequest:
    finding = _finding()
    base: dict[str, object] = {
        "screener_hotkey": _HOTKEY,
        "attempt_id": uuid4(),
        "signature": "ab" * 64,
        "passed": False,
        "outcome": ScreenResultOutcome.QUARANTINE,
        "policy_version": SCREENING_POLICY_VERSION,
        "manifest_digest": "ab" * 32,
        "finding_digest": finding.canonical_digest(),
        "reason_code": "agentic-source-review-tripwire",
        "evidence": [
            ScreenEvidenceItem(
                module_id="luna-source-review",
                code="agentic-source-review-tripwire",
                summary="private source analysis selected a behavioral audit",
                digest=finding.canonical_digest(),
            )
        ],
        "finding": finding,
    }
    base.update(overrides)
    return ScreenResultRequest.model_validate(base)


def test_canonical_digest_is_stable_and_order_insensitive() -> None:
    one = _finding()
    two = SourceReviewFinding.model_validate(one.model_dump(mode="json"))
    assert one.canonical_digest() == two.canonical_digest()
    reordered = one.model_copy(
        update={"categories": list(reversed([*one.categories, "prompt_injection"]))}
    )
    rebuilt = one.model_copy(
        update={"categories": [*one.categories, "prompt_injection"]}
    )
    assert reordered.canonical_digest() == rebuilt.canonical_digest()


def test_quarantine_request_accepts_digest_bound_finding() -> None:
    request = _request()
    assert request.finding is not None
    assert request.finding.canonical_digest() == request.finding_digest


def test_review_settings_binding_is_all_or_nothing() -> None:
    with pytest.raises(ValidationError, match="binding must be complete"):
        _request(review_settings_revision=7)
    request = _request(
        review_settings_revision=7,
        review_settings_instance_id="ditto-screener-prod",
        review_settings_scope="*",
        review_settings_checksum="12" * 32,
    )
    assert request.review_settings_revision == 7


def test_finding_digest_mismatch_is_rejected() -> None:
    with pytest.raises(ValidationError, match="does not match finding_digest"):
        _request(finding_digest="ef" * 32)


def test_finding_without_digest_is_rejected() -> None:
    with pytest.raises(ValidationError, match="finding requires finding_digest"):
        _request(finding_digest=None)


def test_review_notes_require_their_signed_canonical_digest() -> None:
    notes = [
        SourceReviewNote(
            kind="cleared",
            category="general_runtime",
            path="src/lib.rs",
            line=42,
            summary="Reviewed the provider-bound execution path.",
        )
    ]
    request = _request(
        review_notes=notes,
        review_notes_digest=source_review_notes_digest(notes),
    )
    assert request.review_notes == notes

    with pytest.raises(ValidationError, match="do not match review_notes_digest"):
        _request(review_notes=notes, review_notes_digest="ef" * 32)

    with pytest.raises(ValidationError, match="must travel together"):
        _request(review_notes=notes)

    with pytest.raises(ValidationError, match="require manifest_digest"):
        _pass_request(
            review_notes=notes,
            review_notes_digest=source_review_notes_digest(notes),
            manifest_digest=None,
        )


def test_review_payloads_require_review_outcome() -> None:
    with pytest.raises(ValidationError, match="require a review outcome"):
        _request(
            passed=True,
            outcome=ScreenResultOutcome.PASS,
            manifest_digest=None,
            reason_code=None,
            finding=None,
            image_sha256="12" * 32,
            image_size_bytes=123,
            image_id="sha256:" + "34" * 32,
            image_ref="ditto-screen/550e8400-e29b-41d4-a716-446655440000:latest",
            image_upload_id=uuid4(),
        )


@pytest.mark.parametrize("policy_version", [9, SCREENING_POLICY_VERSION])
def test_policy_v9_and_later_reject_legacy_untyped_outcome(
    policy_version: int,
) -> None:
    with pytest.raises(ValidationError, match="requires typed outcome"):
        _request(
            passed=False,
            outcome=None,
            policy_version=policy_version,
            manifest_digest=None,
            finding_digest=None,
            reason_code=None,
            evidence=None,
            finding=None,
        )


def test_policy_v8_retains_legacy_untyped_outcome() -> None:
    request = _request(
        passed=True,
        outcome=None,
        policy_version=8,
        manifest_digest=None,
        finding_digest=None,
        reason_code=None,
        evidence=None,
        finding=None,
    )
    assert request.outcome is None


def test_legacy_outcome_rejects_image_metadata() -> None:
    with pytest.raises(ValidationError, match="legacy result cannot carry"):
        _request(
            passed=True,
            outcome=None,
            policy_version=8,
            manifest_digest=None,
            finding_digest=None,
            reason_code=None,
            evidence=None,
            finding=None,
            image_sha256="12" * 32,
            image_size_bytes=123,
            image_id="sha256:" + "34" * 32,
            image_ref="ditto-screen/550e8400-e29b-41d4-a716-446655440000:latest",
            image_upload_id=uuid4(),
        )


def test_evidence_list_is_bounded() -> None:
    item = ScreenEvidenceItem(
        module_id="m", code="c", summary="bounded public-safe summary"
    )
    with pytest.raises(ValidationError):
        _request(evidence=[item] * 17)


def _pass_request(**overrides: object) -> ScreenResultRequest:
    base: dict[str, object] = {
        "screener_hotkey": _HOTKEY,
        "attempt_id": uuid4(),
        "signature": "ab" * 64,
        "passed": True,
        "outcome": ScreenResultOutcome.PASS,
        "policy_version": SCREENING_POLICY_VERSION,
        "image_sha256": "12" * 32,
        "image_size_bytes": 123,
        "image_id": "sha256:" + "34" * 32,
        "image_ref": "ditto-screen/550e8400-e29b-41d4-a716-446655440000:latest",
        "image_upload_id": uuid4(),
    }
    base.update(overrides)
    return ScreenResultRequest.model_validate(base)


def test_build_only_defaults_false_and_is_omitted_by_legacy_platform() -> None:
    # An un-migrated platform never sends build_only; the model tolerates its
    # absence and defaults to the full-pipeline behavior.
    request = _pass_request()
    assert request.build_only is False


def test_build_only_pass_is_accepted() -> None:
    request = _pass_request(build_only=True)
    assert request.build_only is True
    assert request.outcome == ScreenResultOutcome.PASS


def test_policy_only_pass_reuses_retained_image() -> None:
    request = _pass_request(
        policy_only=True,
        image_sha256=None,
        image_size_bytes=None,
        image_id=None,
        image_ref=None,
        image_upload_id=None,
    )
    assert request.policy_only is True


def test_policy_only_pass_rejects_replacement_image() -> None:
    with pytest.raises(ValidationError, match="must reuse the retained image"):
        _pass_request(policy_only=True)


def test_build_only_result_cannot_quarantine() -> None:
    # A build-only pass skips review, so it can never carry a quarantine.
    with pytest.raises(
        ValidationError, match="build-only result cannot carry a quarantine"
    ):
        _request(build_only=True)


def test_deferred_mechanical_result_can_preserve_oracle_quarantine() -> None:
    request = _request(build_only=True, deferred_source_review=True)
    assert request.deferred_source_review is True
    assert request.outcome == ScreenResultOutcome.QUARANTINE


def test_deferred_source_review_requires_mechanical_lane() -> None:
    with pytest.raises(ValidationError, match="requires the mechanical lane"):
        _pass_request(deferred_source_review=True)
