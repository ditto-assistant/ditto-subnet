"""Cross-language fixtures for sealed shadow-coding evidence upload wires."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from ditto.api_models.coding_evidence_upload import (
    CODING_SEALED_EVIDENCE_MAX_BYTES,
    CodingSealedEvidenceFinalization,
    CodingSealedEvidenceFinalizeRequest,
    CodingSealedEvidenceKind,
    CodingSealedEvidenceUploadCapabilityRequest,
    coding_sealed_evidence_finalize_signing_message,
    coding_sealed_evidence_upload_signing_message,
    parse_coding_sealed_evidence_upload_capability_json,
)

_VECTOR = (
    Path(__file__).parents[3]
    / "packages"
    / "dittobench-coding-contract"
    / "testdata"
    / "coding_sealed_evidence_upload_v1.json"
)


def _vector() -> dict:
    value = json.loads(_VECTOR.read_text(encoding="utf-8"))
    assert value["schema"] == "dittobench-coding-sealed-evidence-upload-vector-v1"
    assert value["coding_contract_version"] == 1
    assert value["weight_eligible"] is False
    return value


def _request_kwargs(request: object) -> dict:
    value = request.model_dump()  # type: ignore[attr-defined]
    value.pop("signature")
    return value


def test_shared_evidence_upload_vector_parses_and_binds_signatures() -> None:
    vector = _vector()
    assert {
        kind.value: maximum
        for kind, maximum in CODING_SEALED_EVIDENCE_MAX_BYTES.items()
    } == {
        item["evidence_kind"]: item["maximum_size_bytes"] for item in vector["policies"]
    }
    capability = parse_coding_sealed_evidence_upload_capability_json(
        json.dumps(vector["capability"])
    )
    assert capability.evidence_kind is CodingSealedEvidenceKind.AUTHORING_TRANSCRIPT
    assert "synthetic-evidence" not in repr(capability)

    upload = CodingSealedEvidenceUploadCapabilityRequest.model_validate_json(
        json.dumps(vector["capability_request"])
    )
    finalize = CodingSealedEvidenceFinalizeRequest.model_validate_json(
        json.dumps(vector["finalization_request"])
    )
    response = CodingSealedEvidenceFinalization.model_validate_json(
        json.dumps(vector["finalization"])
    )
    assert response.accepted is True
    assert response.idempotent is False

    upload_message = coding_sealed_evidence_upload_signing_message(
        **_request_kwargs(upload)
    )
    finalize_message = coding_sealed_evidence_finalize_signing_message(
        **_request_kwargs(finalize)
    )
    expected = vector["expected"]
    assert upload_message.decode() == expected["upload_signing_message_utf8"]
    assert (
        hashlib.sha256(upload_message).hexdigest()
        == expected["upload_signing_message_sha256"]
    )
    assert finalize_message.decode() == expected["finalize_signing_message_utf8"]
    assert (
        hashlib.sha256(finalize_message).hexdigest()
        == expected["finalize_signing_message_sha256"]
    )


def test_capability_ignores_unknown_and_rejects_duplicate_or_identity_drift() -> None:
    raw = _vector()["capability"]
    extended = {**raw, "future_transport_hint": {"ignored": True}}
    parsed = parse_coding_sealed_evidence_upload_capability_json(json.dumps(extended))
    assert "future_transport_hint" not in parsed.model_fields_set

    duplicate = json.dumps(raw).replace(
        '"coding_contract_version": 1',
        '"coding_contract_version": 1, "coding_contract_version": 1',
        1,
    )
    with pytest.raises(ValueError, match="repeats field"):
        parse_coding_sealed_evidence_upload_capability_json(duplicate)

    for mutation in (
        {"checksum_sha256_b64": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="},
        {"size_bytes": 0},
        {"claim_generation": 0},
        {"evidence_kind": "unknown"},
        {"url": raw["url"].replace("authoring-transcript", "frozen-submission")},
        {"expires_at": "2026-09-01T12:31:00Z"},
    ):
        candidate = copy.deepcopy(raw)
        candidate.update(mutation)
        with pytest.raises(ValueError):
            parse_coding_sealed_evidence_upload_capability_json(json.dumps(candidate))


def test_signed_requests_and_finalization_reject_invalid_known_fields() -> None:
    vector = _vector()
    request = copy.deepcopy(vector["capability_request"])
    request["ticket_id"] = "00000000-0000-0000-0000-000000000000"
    with pytest.raises(ValidationError):
        CodingSealedEvidenceUploadCapabilityRequest.model_validate_json(
            json.dumps(request)
        )

    finalize = copy.deepcopy(vector["finalization_request"])
    finalize["size_bytes"] = (512 << 20) + 1
    with pytest.raises(ValidationError):
        CodingSealedEvidenceFinalizeRequest.model_validate_json(json.dumps(finalize))

    response = copy.deepcopy(vector["finalization"])
    response["accepted"] = False
    with pytest.raises(ValidationError):
        CodingSealedEvidenceFinalization.model_validate_json(json.dumps(response))
