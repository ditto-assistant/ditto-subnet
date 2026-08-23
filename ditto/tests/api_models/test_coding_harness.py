from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from ditto.api_models.coding_harness import (
    CodingHarnessLaunchResponse,
    coding_harness_launch_signing_message,
)
from ditto.api_models.coding_inference_grants import CodingInferenceExchangeResponse

_VECTOR = json.loads(
    (
        Path(__file__).resolve().parents[3]
        / "packages"
        / "dittobench-coding-contract"
        / "testdata"
        / "coding_attempt_supervisor_v1.json"
    ).read_text(encoding="utf-8")
)


def test_shared_harness_and_revocation_capabilities_round_trip() -> None:
    harness_raw = _VECTOR["requests"]["author"]["harness"]
    harness = CodingHarnessLaunchResponse.model_validate(harness_raw)
    assert harness.weight_eligible is False
    assert harness.screened_image_size_bytes == 1024
    assert "image_url" not in repr(harness)

    grant_raw = _VECTOR["requests"]["author"]["grant"]
    exchange = CodingInferenceExchangeResponse.model_validate(grant_raw)
    assert exchange.revoke_url.endswith(
        "/api/v1/validator/coding-shadow/inference-revoke-capability"
    )
    assert exchange.revoke_bearer not in repr(exchange)
    assert exchange.bearer not in repr(exchange)


def test_harness_signing_domain_is_exact() -> None:
    requested_at = datetime.fromisoformat("2026-08-23T06:00:00+00:00")
    message = coding_harness_launch_signing_message(
        validator_hotkey="5" * 48,
        ticket_id=UUID("22222222-2222-4222-8222-222222222222"),
        nonce=UUID("11111111-1111-4111-8111-111111111111"),
        requested_at=requested_at,
    )
    assert message == (
        b"dittobench-coding-harness-launch:v1\x00"
        + b"5" * 48
        + b"\x0022222222-2222-4222-8222-222222222222"
        + b"\x0011111111-1111-4111-8111-111111111111"
        + b"\x002026-08-23T06:00:00.000000+00:00"
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("image_url", "http://storage.invalid/image?signature=x"),
        ("screened_image_size_bytes", (8 << 30) + 1),
        ("screened_image_ref", "ditto-screen/foreign:latest"),
        ("expires_at", "2026-08-23T07:00:01Z"),
    ],
)
def test_harness_authority_rejects_transport_and_bound_drift(
    field: str,
    value: object,
) -> None:
    raw = {**_VECTOR["requests"]["author"]["harness"], field: value}
    with pytest.raises(ValidationError):
        CodingHarnessLaunchResponse.model_validate(raw)


def test_revocation_capability_is_required_and_unknown_fields_are_ignored() -> None:
    grant = dict(_VECTOR["requests"]["author"]["grant"])
    grant["future_field"] = {"ignored": True}
    parsed = CodingInferenceExchangeResponse.model_validate(grant)
    assert "future_field" not in parsed.model_fields_set
    grant.pop("revoke_bearer")
    with pytest.raises(ValidationError):
        CodingInferenceExchangeResponse.model_validate(grant)
    equal = dict(_VECTOR["requests"]["author"]["grant"])
    equal["revoke_bearer"] = equal["bearer"]
    with pytest.raises(ValidationError):
        CodingInferenceExchangeResponse.model_validate(equal)
