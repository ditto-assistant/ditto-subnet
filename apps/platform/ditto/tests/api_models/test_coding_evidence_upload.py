"""Platform mirrors the shared sealed-evidence upload vector."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ditto.api_models.coding_evidence_upload import (
    CODING_SEALED_EVIDENCE_MAX_BYTES,
    CodingSealedEvidenceFinalization,
    CodingSealedEvidenceFinalizeRequest,
    CodingSealedEvidenceUploadCapabilityRequest,
    coding_sealed_evidence_finalize_signing_message,
    coding_sealed_evidence_upload_signing_message,
    parse_coding_sealed_evidence_upload_capability_json,
)

_VECTOR = (
    Path(__file__).parents[5]
    / "packages"
    / "dittobench-coding-contract"
    / "testdata"
    / "coding_sealed_evidence_upload_v1.json"
)


def _without_signature(value: object) -> dict:
    result = value.model_dump()  # type: ignore[attr-defined]
    result.pop("signature")
    return result


def test_platform_matches_shared_sealed_evidence_upload_vector() -> None:
    vector = json.loads(_VECTOR.read_text(encoding="utf-8"))
    assert {
        kind.value: maximum
        for kind, maximum in CODING_SEALED_EVIDENCE_MAX_BYTES.items()
    } == {
        item["evidence_kind"]: item["maximum_size_bytes"] for item in vector["policies"]
    }
    capability = parse_coding_sealed_evidence_upload_capability_json(
        json.dumps(vector["capability"])
    )
    assert capability.url == vector["capability"]["url"]
    assert "synthetic-evidence" not in repr(capability)

    upload = CodingSealedEvidenceUploadCapabilityRequest.model_validate(
        vector["capability_request"]
    )
    finalize = CodingSealedEvidenceFinalizeRequest.model_validate(
        vector["finalization_request"]
    )
    response = CodingSealedEvidenceFinalization.model_validate(vector["finalization"])
    assert response.accepted is True

    expected = vector["expected"]
    upload_message = coding_sealed_evidence_upload_signing_message(
        **_without_signature(upload)
    )
    finalize_message = coding_sealed_evidence_finalize_signing_message(
        **_without_signature(finalize)
    )
    assert (
        hashlib.sha256(upload_message).hexdigest()
        == expected["upload_signing_message_sha256"]
    )
    assert (
        hashlib.sha256(finalize_message).hexdigest()
        == expected["finalize_signing_message_sha256"]
    )
