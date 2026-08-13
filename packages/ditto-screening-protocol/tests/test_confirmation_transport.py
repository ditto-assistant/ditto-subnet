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
    ConfirmationInferenceGrantOffer,
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
_BROKER_PUBLIC_KEY = "A" * 43


def _inference_grant_payload(proxy_url: str) -> dict[str, Any]:
    return {
        "lane": "reader",
        "grant_id": _UUID,
        "bearer": "b" * 32,
        "generation": 1,
        "proxy_url": proxy_url,
        "model": "openai/gpt-oss-20b",
        "provider": "openrouter",
        "route_provider": "openrouter",
        "receipt_provider": "openrouter",
        "profile_revision": "longmem-reader-v1",
        "request_budget": 1,
        "token_budget": 1,
        "cost_budget_microusd": 1,
        "expires_at": "2026-08-10T10:00:00Z",
    }


@pytest.mark.parametrize(
    "proxy_url",
    [
        "https://platform.example/api/v1/inference/chat/completions",
        "http://localhost:8000/api/v1/inference/chat/completions",
        "http://127.0.0.1:8000/api/v1/inference/chat/completions",
        "http://[::1]:8000/api/v1/inference/chat/completions",
    ],
)
def test_inference_grant_accepts_https_and_loopback_http(proxy_url: str) -> None:
    grant = ConfirmationInferenceGrantOffer.model_validate(
        _inference_grant_payload(proxy_url)
    )
    assert grant.proxy_url == proxy_url


@pytest.mark.parametrize(
    "proxy_url",
    [
        "http://platform.example/api/v1/inference/chat/completions",
        "ftp://localhost/api/v1/inference/chat/completions",
        "https:///missing-host",
    ],
)
def test_inference_grant_rejects_non_tls_non_loopback_urls(proxy_url: str) -> None:
    with pytest.raises(ValidationError, match="HTTPS or loopback HTTP"):
        ConfirmationInferenceGrantOffer.model_validate(
            _inference_grant_payload(proxy_url)
        )


def _execution_profile_payload() -> dict[str, Any]:
    fixture = json.loads(_PROFILE_FIXTURE_PATH.read_text())
    return {**fixture["profile"], "checksum": fixture["expected_checksum"]}


def _inference_grants() -> list[dict[str, Any]]:
    return [
        {
            "lane": lane,
            "grant_id": _UUID[:-1] + str(index + 2),
            "bearer": f"grant-{lane}-" + "x" * 32,
            "generation": 1,
            "proxy_url": "https://platform.test/confirmation/inference",
            "model": "pinned-model",
            "provider": "pinned-provider",
            "route_provider": "pinned-route",
            "receipt_provider": "pinned-receipt",
            "profile_revision": f"{lane}-profile-v1",
            "request_budget": 1,
            "token_budget": 1,
            "cost_budget_microusd": 1,
            "expires_at": "2026-08-10T10:00:00Z",
        }
        for index, lane in enumerate(("reader", "judge", "embedding"))
    ]


def _transport_cases() -> list[tuple[type[BaseModel], dict[str, Any], str]]:
    raw = {"go_evidence_sha256": _SHA, "latency_ms": 1, "evidence": {}}
    return [
        (
            V9ConfirmationClaimRequest,
            {
                "validator_hotkey": _HOTKEY,
                "slot_id": "longmem-0",
                "profile_revision": "confirmation-v9",
                "profile_checksum": _SHA,
                "broker_public_key": _BROKER_PUBLIC_KEY,
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
                "slot_id": "longmem-0",
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
                "inference_grants": _inference_grants(),
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
        "broker_public_key",
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
        "inference_grants",
    )


def test_claim_json_bytes_preserve_declared_field_order() -> None:
    model, payload, _ = _transport_cases()[0]
    claim = model.model_validate(payload)
    assert claim.model_dump_json() == (
        '{"validator_hotkey":"5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",'
        '"slot_id":"longmem-0","profile_revision":"confirmation-v9",'
        f'"profile_checksum":"{_SHA}",'
        f'"broker_public_key":"{_BROKER_PUBLIC_KEY}",'
        f'"nonce":"{_UUID}","requested_at":"2026-08-10T10:00:00Z",'
        '"signature":"aa"}'
    )
