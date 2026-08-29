"""Contract tests for the separate shadow coding evaluation ledger."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from ditto.api_models.coding_evaluation import (
    CodingRunEvidence,
    SubmitCodingShadowResultRequest,
    coding_run_evidence_digest,
    coding_shadow_result_signing_message,
)


def _vectors() -> dict:
    return json.loads(
        (
            Path(__file__).parents[5]
            / "packages"
            / "dittobench-coding-contract"
            / "testdata"
            / "coding_contract_v1.json"
        ).read_text(encoding="utf-8")
    )


def test_platform_run_evidence_matches_shared_contract_vector() -> None:
    vectors = _vectors()
    evidence = CodingRunEvidence.model_validate_json(
        json.dumps(vectors["run_evidence"])
    )
    assert coding_run_evidence_digest(evidence) == vectors["digests"]["run_evidence"]
    extended = {**vectors["run_evidence"], "future_diagnostic": "ignored"}
    assert coding_run_evidence_digest(
        CodingRunEvidence.model_validate_json(json.dumps(extended))
    ) == coding_run_evidence_digest(evidence)

    changed = evidence.model_dump(mode="json", by_alias=True)
    changed["repair_mean_micros"] = 0
    with pytest.raises(ValidationError, match="aggregate"):
        CodingRunEvidence.model_validate_json(json.dumps(changed))


def test_result_envelope_binds_digest_deadline_and_signature_message() -> None:
    evidence = CodingRunEvidence.model_validate_json(
        json.dumps(_vectors()["run_evidence"])
    )
    digest = coding_run_evidence_digest(evidence)
    agent_id = UUID("11111111-1111-4111-8111-111111111111")
    run_row_id = UUID("22222222-2222-4222-8222-222222222222")
    ticket_id = UUID("33333333-3333-4333-8333-333333333333")
    deadline = datetime(2026, 8, 21, 12, tzinfo=UTC)
    payload = {
        "validator_hotkey": "5" + "A" * 47,
        "bench_version": 12,
        "run_row_id": run_row_id,
        "ticket_id": ticket_id,
        "ticket_deadline": deadline,
        "agent_artifact_sha256": "cd" * 32,
        "screened_image_sha256": "ab" * 32,
        "run_evidence_sha256": digest,
        "evidence": evidence,
        "signature": "12" * 64,
    }
    assert (
        SubmitCodingShadowResultRequest.model_validate(payload).ticket_id == ticket_id
    )
    assert coding_shadow_result_signing_message(
        validator_hotkey=str(payload["validator_hotkey"]),
        agent_id=agent_id,
        run_row_id=run_row_id,
        ticket_id=ticket_id,
        bench_version=12,
        ticket_deadline=deadline,
        agent_artifact_sha256="cd" * 32,
        screened_image_sha256="ab" * 32,
        run_evidence_sha256=digest,
    ) == (
        b"dittobench-coding-shadow-result:v1\x00"
        + ("5" + "A" * 47).encode()
        + b"\x0011111111-1111-4111-8111-111111111111"
        + b"\x0022222222-2222-4222-8222-222222222222"
        + b"\x0033333333-3333-4333-8333-333333333333"
        + b"\x0012\x002026-08-21T12:00:00.000000+00:00"
        + b"\x00"
        + ("cd" * 32).encode()
        + b"\x00"
        + ("ab" * 32).encode()
        + b"\x00"
        + digest.encode()
    )

    payload["run_evidence_sha256"] = "ff" * 32
    with pytest.raises(ValidationError, match="does not match"):
        SubmitCodingShadowResultRequest.model_validate(payload)
