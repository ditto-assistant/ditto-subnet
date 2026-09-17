"""Historical review feedback is projected without changing a verdict."""

from uuid import uuid4

import pytest

from ditto.api_server.endpoints.public import _public_terminal_screening_review
from ditto.db.models import ScreeningAttempt, ScreeningQuarantine
from ditto_screening_protocol.models import (
    SourceReviewFinding,
    SourceReviewInvariant,
    SourceReviewNote,
    source_review_notes_digest,
)


def test_v13_invariant_reasoning_keeps_citation_indices():
    attempt, quarantine, _ = _historical_adjudicated_rejection()
    finding = SourceReviewFinding.model_validate(
        {
            "artifact_sha256": "ab" * 32,
            "prompt_revision": "synthetic-policy-v13",
            "risk_level": "high",
            "confidence": 0.99,
            "categories": ["benchmark_emulation"],
            "evidence": [
                {"path": "src/answer.rs", "line": 37, "category": "benchmark_emulation"}
            ],
            "summary": "Bounded source finding.",
            "invariant_assessment": {
                "schema_version": 2,
                "decisions": [
                    {
                        "invariant": invariant.value,
                        "disposition": "breach",
                        "summary": "The served fallback overrides the policy boundary.",
                        "evidence_indices": [0],
                    }
                    for invariant in SourceReviewInvariant
                ],
            },
        }
    )
    quarantine.finding = finding.model_dump(mode="json")
    quarantine.finding_digest = finding.canonical_digest()
    projected = _public_terminal_screening_review(
        quarantine, artifact_sha256="ab" * 32, attempt=attempt
    )[1]
    assert projected is not None
    assert projected.invariant_assessment is not None
    assert len(projected.invariant_assessment.decisions) == 8
    assert projected.invariant_assessment.decisions[0].evidence_indices == [0]
    assert projected.locations[0].path == "src/answer.rs"
    assert "artifact_sha256" not in projected.model_dump()
    quarantine.finding["invariant_assessment"]["decisions"][0]["summary"] = "Tampered"
    assert (
        _public_terminal_screening_review(
            quarantine, artifact_sha256="ab" * 32, attempt=attempt
        )[1]
        is None
    )


def test_unknown_private_fields_never_enter_public_note_projection():
    attempt, quarantine, _ = _historical_adjudicated_rejection()
    quarantine.review_notes[0]["private_prompt"] = "secret"
    projected = _public_terminal_screening_review(
        quarantine, artifact_sha256="ab" * 32, attempt=attempt
    )[2]
    assert projected
    assert "private_prompt" not in projected[0].model_dump()


def _historical_adjudicated_rejection():
    agent_id, attempt_id = uuid4(), uuid4()
    notes = [
        SourceReviewNote(
            kind="concern",
            category="benchmark_emulation",
            path="src/answer.rs",
            line=37,
            summary="The served fallback replaces the model answer with a local table.",
        ),
        SourceReviewNote(
            kind="cleared",
            summary="The retrieval path retains the full current records.",
        ),
    ]
    attempt = ScreeningAttempt(
        agent_id=agent_id,
        attempt_id=attempt_id,
        policy_version=13,
        status="rejected",
        reason_code="adjudicated-source-review-reject",
    )
    quarantine = ScreeningQuarantine(
        agent_id=agent_id,
        attempt_id=attempt_id,
        policy_version=13,
        status="resolved",
        resolution="rescreen",
        reason_code="adjudicated-source-review-reject",
        finding=None,
        review_notes=[note.model_dump(mode="json") for note in notes],
        review_notes_digest=source_review_notes_digest(notes),
    )
    return attempt, quarantine, notes


def test_historical_adjudicated_rejection_without_finding_restores_notes():
    attempt, quarantine, notes = _historical_adjudicated_rejection()
    evidence, finding, projected = _public_terminal_screening_review(
        quarantine, artifact_sha256="ab" * 32, attempt=attempt
    )
    assert finding is None
    assert evidence == []
    assert [note.model_dump() for note in projected] == [
        note.model_dump() for note in notes
    ]
    assert projected[0].path == "src/answer.rs"
    assert projected[1].kind == "cleared"
    assert quarantine.resolution == "rescreen"
    assert attempt.status == "rejected"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("status", "active"),
        ("resolution", "release"),
        ("reason_code", "source-review-inconclusive"),
        ("agent_id", uuid4()),
        ("attempt_id", uuid4()),
        ("policy_version", 12),
        ("review_notes_digest", "00" * 32),
        ("review_notes", [{"kind": "concern", "summary": ""}]),
    ],
)
def test_unbound_or_nonterminal_notes_remain_private(field, value):
    attempt, quarantine, _ = _historical_adjudicated_rejection()
    setattr(quarantine, field, value)
    assert (
        _public_terminal_screening_review(
            quarantine, artifact_sha256="ab" * 32, attempt=attempt
        )[2]
        == []
    )


@pytest.mark.parametrize("status", ["running", "quarantined", "passed", "failed"])
def test_rescreen_is_not_itself_a_rejection(status):
    attempt, quarantine, _ = _historical_adjudicated_rejection()
    attempt.status = status
    assert _public_terminal_screening_review(
        quarantine, artifact_sha256="ab" * 32, attempt=attempt
    ) == ([], None, [])


def test_tampered_note_text_is_not_published():
    attempt, quarantine, _ = _historical_adjudicated_rejection()
    quarantine.review_notes[0]["summary"] = "Injected text"
    assert (
        _public_terminal_screening_review(
            quarantine, artifact_sha256="ab" * 32, attempt=attempt
        )[2]
        == []
    )
