from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from ditto.api_models.coding_inference_grants import (
    CodingInferenceExchangeResponse,
    CodingInferenceGrantOffer,
)

_ROOT = Path(__file__).parents[5]


def _authority() -> dict[str, object]:
    return {
        "coding_contract_version": 1,
        "weight_eligible": False,
        "grant_id": UUID("44444444-4444-4444-8444-444444444444"),
        "ticket_id": UUID("33333333-3333-4333-8333-333333333333"),
        "run_row_id": UUID("55555555-5555-4555-8555-555555555555"),
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
        "expires_at": datetime(2026, 8, 22, 19, tzinfo=UTC),
    }


def test_platform_and_validator_grant_contracts_are_exact_mirrors() -> None:
    validator = _ROOT / "ditto/api_models/coding_inference_grants.py"
    platform = _ROOT / "apps/platform/ditto/api_models/coding_inference_grants.py"
    assert validator.read_bytes() == platform.read_bytes()


def test_platform_grant_models_project_locked_shadow_authority() -> None:
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
        }
    )
    exchange = CodingInferenceExchangeResponse.model_validate(
        {
            "schema": "dittobench-coding-inference-exchange-v1",
            **_authority(),
            "expires_at": offer.expires_at + timedelta(0),
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
    assert exchange.weight_eligible is False
    assert exchange.inference_grant_sha256 == offer.inference_grant_sha256
