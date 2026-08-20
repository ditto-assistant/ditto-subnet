from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from ditto.api_models.coding_certification import (
    CodingCapabilityCertificationReceipt,
    SubmitCodingCertificationRequest,
    coding_certification_receipt_digest,
    coding_certification_signing_message,
)

_VECTOR_PATH = (
    Path(__file__).parents[5]
    / "packages/dittobench-coding-contract/testdata/coding_certification_v1.json"
)


def _vector() -> dict:
    return json.loads(_VECTOR_PATH.read_text(encoding="utf-8"))


def test_platform_certification_contract_matches_shared_vector() -> None:
    vector = _vector()
    receipt = CodingCapabilityCertificationReceipt.model_validate_json(
        json.dumps(vector["receipt"])
    )
    expected = vector["expected"]
    assert (
        coding_certification_receipt_digest(receipt) == expected["certification_sha256"]
    )
    message = coding_certification_signing_message(
        validator_hotkey=expected["validator_hotkey"],
        agent_id=UUID(expected["agent_id"]),
        bench_version=expected["bench_version"],
        ticket_deadline=datetime.fromisoformat(expected["ticket_deadline"]),
        screened_image_sha256=expected["screened_image_sha256"],
        certification_sha256=receipt.certification_sha256,
    )
    assert hashlib.sha256(message).hexdigest() == expected["signing_message_sha256"]


def test_platform_certification_envelope_requires_aware_ticket_deadline() -> None:
    vector = _vector()
    expected = vector["expected"]
    payload = {
        "validator_hotkey": expected["validator_hotkey"],
        "bench_version": expected["bench_version"],
        "ticket_deadline": expected["ticket_deadline"],
        "screened_image_sha256": expected["screened_image_sha256"],
        "receipt": vector["receipt"],
        "signature": "00" * 64,
    }
    SubmitCodingCertificationRequest.model_validate_json(json.dumps(payload))
    payload["ticket_deadline"] = "2026-08-20T16:30:00"
    with pytest.raises(ValidationError, match="timezone-aware"):
        SubmitCodingCertificationRequest.model_validate_json(json.dumps(payload))


def test_platform_certification_known_fields_are_strict_and_forward_compatible() -> (
    None
):
    receipt = _vector()["receipt"]
    receipt["future_diagnostic"] = {"ignored": True}
    CodingCapabilityCertificationReceipt.model_validate_json(json.dumps(receipt))

    missing = copy.deepcopy(receipt)
    del missing["weight_eligible"]
    with pytest.raises(ValidationError):
        CodingCapabilityCertificationReceipt.model_validate_json(json.dumps(missing))

    weighted = copy.deepcopy(receipt)
    weighted["weight_eligible"] = True
    with pytest.raises(ValidationError):
        CodingCapabilityCertificationReceipt.model_validate_json(json.dumps(weighted))
