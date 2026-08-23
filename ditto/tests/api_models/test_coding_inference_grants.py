from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from pydantic import ValidationError

from ditto.api_models.coding_inference_grants import (
    CodingInferenceExchangeRequest,
    CodingInferenceExchangeResponse,
    CodingInferenceGrantOffer,
    CodingInferenceGrantRequest,
    CodingInferenceRevokeRequest,
    CodingInferenceRevokeResponse,
    coding_inference_exchange_signing_message,
    coding_inference_grant_signing_message,
    coding_inference_revoke_signing_message,
)

_NOW = datetime(2026, 8, 22, 18, 30, 0, 123456, tzinfo=UTC)
_HOTKEY = "5" + "V" * 47
_TICKET = UUID("33333333-3333-4333-8333-333333333333")
_GRANT = UUID("44444444-4444-4444-8444-444444444444")
_RUN = UUID("55555555-5555-4555-8555-555555555555")
_NONCE = UUID("66666666-6666-4666-8666-666666666666")
_BROKER = "A" * 43


def _authority() -> dict[str, object]:
    return {
        "coding_contract_version": 1,
        "weight_eligible": False,
        "grant_id": _GRANT,
        "ticket_id": _TICKET,
        "run_row_id": _RUN,
        "case_id": "private-case-001",
        "profile_capability_id": "private-profile-001",
        "inference_grant_sha256": "11" * 32,
        "model": "openai/gpt-5.6-luna",
        "provider_api": "openrouter",
        "provider_route": "azure/eu",
        "receipt_provider": "Azure",
        "provider_route_profile": "luna-azure-eu-zdr-v1",
        "provider_account_guardrail": "openrouter_private_account_v1",
        "provider_pipeline_policy": "no_plugins_no_transforms_v1",
        "provider_cache_policy": "disabled_v1",
        "reasoning_effort": "medium",
        "request_budget": 100,
        "prompt_token_budget": 200_000,
        "completion_token_budget": 30_000,
        "cost_budget_usd_micros": 10_000_000,
        "expires_at": _NOW + timedelta(hours=1),
    }


def test_grant_offer_exchange_and_revocation_are_strict_and_forward_compatible() -> (
    None
):
    offer = CodingInferenceGrantOffer.model_validate(
        {
            "schema": "dittobench-coding-inference-grant-offer-v1",
            **_authority(),
            "status": "pending",
            "generation": 0,
            "exchange_url": (
                "https://platform.invalid/api/v1/validator/"
                "coding-shadow/inference-exchange"
            ),
            "future_field": "ignored",
        }
    )
    assert offer.model_dump(mode="json", by_alias=True)["schema"].endswith(
        "grant-offer-v1"
    )
    exchange = CodingInferenceExchangeResponse.model_validate(
        {
            "schema": "dittobench-coding-inference-exchange-v1",
            **_authority(),
            "status": "active",
            "generation": 1,
            "bearer": "b" * 43,
            "proxy_url": (
                "https://relay.invalid/api/v1/inference/coding/chat/completions"
            ),
            "revoke_bearer": "r" * 43,
            "revoke_url": (
                "https://platform.invalid/api/v1/validator/"
                "coding-shadow/inference-revoke-capability"
            ),
        }
    )
    assert exchange.generation == 1
    assert "b" * 43 not in repr(exchange)
    revoked = CodingInferenceRevokeResponse(
        schema="dittobench-coding-inference-revocation-v1",
        coding_contract_version=1,
        weight_eligible=False,
        grant_id=_GRANT,
        ticket_id=_TICKET,
        status="revoked",
        generation=1,
        revoked_at=_NOW,
        idempotent=False,
    )
    assert revoked.status == "revoked"


@pytest.mark.parametrize(
    "field,value",
    [
        (
            "exchange_url",
            "http://platform.invalid/api/v1/validator/coding-shadow/inference-exchange",
        ),
        (
            "exchange_url",
            "https://user:pass@platform.invalid/api/v1/validator/coding-shadow/inference-exchange",
        ),
        (
            "exchange_url",
            "https://platform.invalid/api/v1/validator/coding-shadow/inference-exchange?secret=1",
        ),
        ("status", "revoked"),
        ("generation", 1),
    ],
)
def test_grant_offer_rejects_unsafe_transport_or_state(
    field: str, value: object
) -> None:
    values = {
        "schema": "dittobench-coding-inference-grant-offer-v1",
        **_authority(),
        "status": "pending",
        "generation": 0,
        "exchange_url": (
            "https://platform.invalid/api/v1/validator/coding-shadow/inference-exchange"
        ),
    }
    values[field] = value
    with pytest.raises(ValidationError):
        CodingInferenceGrantOffer.model_validate(values)


def test_signed_requests_reject_nil_and_naive_authority() -> None:
    request = CodingInferenceGrantRequest(
        validator_hotkey=_HOTKEY,
        ticket_id=_TICKET,
        nonce=_NONCE,
        requested_at=_NOW,
        signature="aa" * 64,
    )
    assert request.ticket_id == _TICKET
    with pytest.raises(ValidationError):
        CodingInferenceGrantRequest(
            validator_hotkey=_HOTKEY,
            ticket_id=UUID(int=0),
            nonce=_NONCE,
            requested_at=_NOW,
            signature="aa" * 64,
        )
    with pytest.raises(ValidationError):
        CodingInferenceExchangeRequest(
            validator_hotkey=_HOTKEY,
            grant_id=_GRANT,
            broker_public_key=_BROKER,
            nonce=_NONCE,
            requested_at=_NOW.replace(tzinfo=None),
            signature="aa" * 64,
        )
    with pytest.raises(ValidationError):
        CodingInferenceRevokeRequest(
            validator_hotkey=_HOTKEY,
            grant_id=_GRANT,
            generation=True,
            nonce=_NONCE,
            requested_at=_NOW,
            signature="aa" * 64,
        )


def test_signing_messages_are_domain_separated_and_exact() -> None:
    grant = coding_inference_grant_signing_message(
        validator_hotkey=_HOTKEY,
        ticket_id=_TICKET,
        nonce=_NONCE,
        requested_at=_NOW,
    )
    exchange = coding_inference_exchange_signing_message(
        validator_hotkey=_HOTKEY,
        grant_id=_GRANT,
        broker_public_key=_BROKER + "=",
        nonce=_NONCE,
        requested_at=_NOW,
    )
    revoke = coding_inference_revoke_signing_message(
        validator_hotkey=_HOTKEY,
        grant_id=_GRANT,
        generation=1,
        nonce=_NONCE,
        requested_at=_NOW,
    )
    assert len({grant, exchange, revoke}) == 3
    assert exchange.split(b":")[4] == _BROKER.encode()
    assert all(
        value.endswith(b"2026-08-22T18:30:00.123456+00:00")
        for value in (grant, exchange, revoke)
    )
