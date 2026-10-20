"""A v13 court decision travels only as a held quarantine."""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from ditto_screening_protocol import (
    AdjudicationCompletionReceipt,
    ScreenResultOutcome,
    ScreenResultRequest,
    SourceReviewAdjudication,
    SourceReviewCitation,
    SourceReviewInvariant,
)

_RECEIPT = AdjudicationCompletionReceipt(
    elapsed_ms=4300,
    first_tool_call_ms=2000,
    first_tool_observation="stream_delta",
    observed_model="z-ai/glm-5.3-flash",
    gateway_provider="openrouter",
    observed_upstream="together",
    request_count=1,
    final_request_prompt_bytes=8000,
    final_request_wire_bytes=700,
    final_request_event_count=4,
    prompt_tokens=200,
    completion_tokens=80,
)


def _adjudication(decision: str) -> SourceReviewAdjudication:
    verdict: dict[str, object] = (
        {"clear_clause": "model_authors_graded_slot"}
        if decision == "clear"
        else {"reject_invariant": SourceReviewInvariant.EVALUATION_INDEPENDENCE}
    )
    return SourceReviewAdjudication(
        decision=decision,
        reason="The cited served path decides the graded answer.",
        citations=[SourceReviewCitation(path="src/main.rs", line=6)],
        model="z-ai/glm-5.3-flash",
        prompt_revision="adjudicator-v7-policy-v13",
        completion_receipt=_RECEIPT,
        **verdict,
    )


def _request(
    policy_version: int, decision: str, outcome: ScreenResultOutcome
) -> ScreenResultRequest:
    adjudication = _adjudication(decision)
    passed = outcome == ScreenResultOutcome.PASS
    image = (
        {
            "image_sha256": "12" * 32,
            "image_size_bytes": 123,
            "image_id": "sha256:" + "34" * 32,
            "image_ref": "ditto-screen/550e8400-e29b-41d4-a716-446655440000:latest",
            "image_upload_id": uuid4(),
        }
        if passed
        else {}
    )
    return ScreenResultRequest.model_validate(
        {
            "screener_hotkey": "5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm",
            "attempt_id": uuid4(),
            "signature": "ab" * 64,
            "passed": passed,
            "outcome": outcome,
            "policy_version": policy_version,
            "manifest_digest": "ab" * 32,
            "reason_code": "source-review-awaiting-v13-verification",
            "review_settings_revision": 1,
            "review_settings_instance_id": "ditto-screener-prod",
            "review_settings_scope": "*",
            "review_settings_checksum": "cd" * 32,
            "adjudication": adjudication,
            "adjudication_digest": adjudication.canonical_digest(),
            "completion_receipt_signature": "ef" * 64,
            **image,
        }
    )


@pytest.mark.parametrize(
    ("policy_version", "decision", "outcome"),
    [
        (13, "clear", ScreenResultOutcome.QUARANTINE),
        (13, "reject", ScreenResultOutcome.QUARANTINE),
        (12, "clear", ScreenResultOutcome.PASS),
        (12, "clear", ScreenResultOutcome.QUARANTINE),
    ],
)
def test_court_decision_accepts_supported_transport(
    policy_version: int, decision: str, outcome: ScreenResultOutcome
) -> None:
    request = _request(policy_version, decision, outcome)

    assert request.adjudication is not None
    assert request.adjudication.decision == decision
    assert request.adjudication.completion_receipt == _RECEIPT


@pytest.mark.parametrize(
    ("policy_version", "decision", "message"),
    [
        (13, "clear", "v13 adjudicated clear requires quarantine transport"),
        (13, "reject", "adjudicated reject requires quarantine transport"),
        (12, "reject", "adjudicated reject requires quarantine transport"),
    ],
)
def test_court_decision_refuses_pass_transport(
    policy_version: int, decision: str, message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        _request(policy_version, decision, ScreenResultOutcome.PASS)
