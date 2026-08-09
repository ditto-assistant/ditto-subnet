"""Semantic guards for the canonical private confirmation transport."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from ditto_screening_protocol.confirmation_transport import (
    ConfirmationAblationCoordinatorProfile,
    ConfirmationExecutionProfile,
    V9ConfirmationClaimRequest,
    V9ConfirmationFailRequest,
    V9ConfirmationJobResponse,
    V9ConfirmationPrepareRequest,
)

_PROFILE_FIXTURE_PATH = (
    Path(__file__).resolve().parents[3]
    / "services"
    / "dittobench-api"
    / "cmd"
    / "dittobench-api"
    / "testdata"
    / "confirmation_execution_profile_v9.json"
)
_HOTKEY = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
_UUID = "10000000-0000-0000-0000-000000000001"
_SHA = "a" * 64


def _execution_profile_payload() -> dict[str, Any]:
    fixture = json.loads(_PROFILE_FIXTURE_PATH.read_text())
    return {**fixture["profile"], "checksum": fixture["expected_checksum"]}


def _transport_cases() -> list[tuple[type[BaseModel], dict[str, Any], str]]:
    raw = {"go_evidence_sha256": _SHA, "latency_ms": 1, "evidence": {}}
    return [
        (
            V9ConfirmationClaimRequest,
            {
                "validator_hotkey": _HOTKEY,
                "slot_id": "slot-0",
                "profile_revision": "confirmation-v9",
                "profile_checksum": _SHA,
                "nonce": _UUID,
                "requested_at": "2026-08-10T10:00:00Z",
                "signature": "aa",
            },
            "requested_at",
        ),
        (
            V9ConfirmationJobResponse,
            {
                "purpose": "v9_confirmation_bundle",
                "bundle_id": _UUID,
                "ticket_id": _UUID,
                "reservation_id": _UUID,
                "agent_id": _UUID,
                "slot_id": "slot-0",
                "deadline": "2026-08-10T10:00:00Z",
                "artifact_sha256": _SHA,
                "bench_version": 9,
                "settings_revision": 1,
                "settings_checksum": _SHA,
                "retest_generation": 0,
                "mode": "shadow",
                "per_bundle_request_cap": 1,
                "per_bundle_token_cap": 1,
                "execution_profile": _execution_profile_payload(),
            },
            "deadline",
        ),
        (
            V9ConfirmationPrepareRequest,
            {
                "validator_hotkey": _HOTKEY,
                "ticket_id": _UUID,
                "nonce": _UUID,
                "requested_at": "2026-08-10T10:00:00Z",
                "wire_sha256": _SHA,
                "ablation_coordinator_latency_ms": 1,
                "longmemeval": raw,
                "inference_ablation": raw,
                "embedding_ablation": raw,
                "signature": "aa",
            },
            "requested_at",
        ),
        (
            V9ConfirmationFailRequest,
            {
                "validator_hotkey": _HOTKEY,
                "ticket_id": _UUID,
                "reason": "infrastructure",
                "nonce": _UUID,
                "requested_at": "2026-08-10T10:00:00Z",
                "signature": "aa",
            },
            "requested_at",
        ),
    ]


@pytest.mark.parametrize(("model", "payload", "timestamp_field"), _transport_cases())
def test_transport_accepts_json_timestamps_only_when_timezone_aware(
    model: type[BaseModel], payload: dict[str, Any], timestamp_field: str
) -> None:
    accepted = model.model_validate_json(json.dumps(payload))
    assert getattr(accepted, timestamp_field).utcoffset() is not None

    naive = copy.deepcopy(payload)
    naive[timestamp_field] = "2026-08-10T10:00:00"
    with pytest.raises(ValidationError, match="must include a timezone"):
        model.model_validate_json(json.dumps(naive))


@pytest.mark.parametrize(
    ("update", "message"),
    [
        ({"max_requests": 5}, "request cap is inconsistent"),
        ({"max_requests": 7}, "request cap is inconsistent"),
        (
            {"request_timeout_milliseconds": 101},
            "total timeout is shorter than one request",
        ),
    ],
)
def test_coordinator_rejects_caps_outside_one_shared_semantic_envelope(
    update: dict[str, int], message: str
) -> None:
    payload = {
        "sample_size": 2,
        "max_attempts": 1,
        "max_requests": 6,
        "request_timeout_milliseconds": 50,
        "total_timeout_milliseconds": 100,
        **update,
    }
    with pytest.raises(ValidationError, match=message):
        ConfirmationAblationCoordinatorProfile.model_validate(payload)


@pytest.mark.parametrize(
    ("first", "second", "message"),
    [
        ("embedding", "embedding", "inference_ablation"),
        ("inference", "inference", "embedding_ablation"),
    ],
)
def test_execution_profile_rejects_swapped_or_duplicated_ablation_roles(
    first: str, second: str, message: str
) -> None:
    payload = _execution_profile_payload()
    payload["inference_ablation"]["intervention"] = first
    payload["embedding_ablation"]["intervention"] = second
    with pytest.raises(ValidationError, match=message):
        ConfirmationExecutionProfile.model_validate(payload)


def test_transport_field_order_remains_wire_compatible() -> None:
    assert tuple(V9ConfirmationClaimRequest.model_fields) == (
        "validator_hotkey",
        "slot_id",
        "profile_revision",
        "profile_checksum",
        "nonce",
        "requested_at",
        "signature",
    )
    assert tuple(V9ConfirmationJobResponse.model_fields) == (
        "purpose",
        "bundle_id",
        "ticket_id",
        "reservation_id",
        "agent_id",
        "slot_id",
        "deadline",
        "artifact_sha256",
        "bench_version",
        "settings_revision",
        "settings_checksum",
        "retest_generation",
        "mode",
        "per_bundle_request_cap",
        "per_bundle_token_cap",
        "execution_profile",
    )


def test_claim_json_bytes_preserve_declared_field_order() -> None:
    model, payload, _ = _transport_cases()[0]
    claim = model.model_validate(payload)
    assert claim.model_dump_json() == (
        '{"validator_hotkey":"5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",'
        '"slot_id":"slot-0","profile_revision":"confirmation-v9",'
        f'"profile_checksum":"{_SHA}",'
        f'"nonce":"{_UUID}","requested_at":"2026-08-10T10:00:00Z",'
        '"signature":"aa"}'
    )
