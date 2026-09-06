from __future__ import annotations

import hashlib
import json
import time
from fractions import Fraction
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select

from ditto.api_models.coding_hosted_budget import HostedBudgetProfile
from ditto.api_models.coding_hosted_inference import HostedInferencePolicy
from ditto.api_server.coding_hosted_budget import (
    HostedBudgetError,
    ProfiledBudgetEstimator,
)
from ditto.api_server.coding_hosted_provider import (
    HostedProviderAdapter,
    HostedProviderError,
)
from ditto.db.models import CodingHostedInferenceRequest
from ditto.tests.api_server.test_coding_hosted_inference import (
    fixture,
    locked_body,
    native_policy,
)
from ditto.tests.api_server.test_coding_hosted_provider import (
    SyntheticEstimator,
    http_response,
    response_body,
)


def budget_profile(**changes):
    value = {
        "schema": "dittobench-coding-hosted-budget-profile-v2",
        "algorithm": "provider-billed-input-cap-v1",
        "model": "openai/gpt-5.6-luna",
        "provider_api": "openrouter",
        "provider_route": "azure/eu",
        "provider_route_profile": "luna-azure-eu-zdr-v1",
        "provider_review_sha256": "a" * 64,
        "token_accounting_review_sha256": "b" * 64,
        "pricing_review_sha256": "c" * 64,
        "valid_from_unix": 1000,
        "valid_until_unix": 4600,
        "max_billed_prompt_tokens_per_request": 100,
        "max_completion_tokens_per_request": 100,
        "prompt_price_usd_nanos_per_token": 201,
        "completion_price_usd_nanos_per_token": 1201,
        "fixed_charge_usd_micros_per_request": 7,
    }
    value.update(changes)
    return HostedBudgetProfile.model_validate(value)


def estimator(profile=None, *, now=lambda: 1001):
    profile = profile or budget_profile()
    policy = native_policy(runtime_profile_sha256=profile.digest())
    return ProfiledBudgetEstimator(
        profile_bytes=profile.canonical_bytes(), policy=policy, now=now
    )


def test_profile_digest_and_legacy_policy_compatibility():
    profile = budget_profile()
    assert (
        profile.digest()
        == "31d3b16bc3fac57118a21f53deb1d3ce5b12cb2e7c6af0f57609c10e44e38f51"
    )
    assert hashlib.sha256(profile.canonical_bytes()).hexdigest() == profile.digest()
    policy = native_policy()
    value = policy.model_dump(mode="json", by_alias=True)
    assert "runtime_profile_sha256" not in value
    assert (
        HostedInferencePolicy.model_validate(
            {**value, "runtime_profile_sha256": None}
        ).digest()
        == policy.digest()
    )
    assert (
        native_policy(runtime_profile_sha256=profile.digest()).digest()
        != policy.digest()
    )
    extra = json.loads(profile.canonical_bytes())
    extra["future_advisory"] = "not authority"
    assert HostedBudgetProfile.model_validate(extra).digest() == profile.digest()


@pytest.mark.parametrize("tokens,expected", [(1, 30), (10, 41), (99, 147), (100, 149)])
def test_integer_cost_conformance(tokens, expected):
    profile = budget_profile()
    body = json.loads(locked_body())
    body["max_completion_tokens"] = tokens
    result = estimator(profile).ceilings(json.dumps(body).encode())
    assert result.prompt_tokens == 100  # Full reviewed cap, not a guessed count.
    assert result.completion_tokens == tokens
    assert result.cost_usd_micros == expected
    exact = Fraction(100 * 201 + tokens * 1201, 1000) + 7
    assert result.cost_usd_micros >= exact


@pytest.mark.parametrize(
    "changes",
    [
        {"valid_until_unix": 1000},
        {"valid_until_unix": 87401},
        {"max_billed_prompt_tokens_per_request": True},
        {"max_billed_prompt_tokens_per_request": 0},
        {"prompt_price_usd_nanos_per_token": "201"},
        {"prompt_price_usd_nanos_per_token": 1.5},
        {"prompt_price_usd_nanos_per_token": 0},
        {"fixed_charge_usd_micros_per_request": -1},
        {
            "prompt_price_usd_nanos_per_token": 1_000_000_000,
            "max_billed_prompt_tokens_per_request": 2_250_000,
        },
        {"provider_route": "azure/us"},
        {"algorithm": "chars-divided-by-four"},
    ],
)
def test_profile_rejects_unreviewed_shapes(changes):
    with pytest.raises(ValidationError):
        budget_profile(**changes)


@pytest.mark.parametrize("clock", [999, 4600, 4601, float("nan"), float("inf"), True])
def test_expired_future_and_invalid_clocks_fail(clock):
    with pytest.raises(HostedBudgetError):
        estimator(now=lambda: clock)


def test_clock_rollback_permanently_closes_estimator():
    clock = [1100]
    value = estimator(now=lambda: clock[0])
    clock[0] = 1099
    with pytest.raises(HostedBudgetError):
        value.ceilings(locked_body())
    clock[0] = 1101
    with pytest.raises(HostedBudgetError):
        value.ceilings(locked_body())


def test_profile_and_policy_changes_are_rejected():
    profile = budget_profile()
    for policy in (
        native_policy(),
        native_policy(runtime_profile_sha256="f" * 64),
        native_policy(runtime_profile_sha256=profile.digest(), max_cost_usd_micros=148),
    ):
        with pytest.raises(HostedBudgetError):
            ProfiledBudgetEstimator(
                profile_bytes=profile.canonical_bytes(), policy=policy, now=lambda: 1001
            )
    with pytest.raises(HostedBudgetError):
        estimator().require_policy(
            native_policy(runtime_profile_sha256=profile.digest(), max_requests=3)
        )


async def profiled_fixture(maker):
    now = int(time.time())
    profile = budget_profile(valid_from_unix=now - 1, valid_until_unix=now + 3600)
    authority, worker, policy, execution, ledger, grant = await fixture(
        maker, runtime_profile_sha256=profile.digest()
    )
    estimate = ProfiledBudgetEstimator(
        profile_bytes=profile.canonical_bytes(), policy=policy
    )
    return authority, worker, policy, execution, ledger, grant, estimate


async def test_real_profile_and_ledger_refund_only_verified_usage(session_maker):
    _, _, policy, _, ledger, grant, estimate = await profiled_fixture(session_maker)
    adapter = HostedProviderAdapter(
        ledger=ledger,
        grant_id=grant,
        policy=policy,
        estimator=estimate,
        api_key="synthetic",
        _test_transport=httpx.MockTransport(lambda _r: http_response(response_body())),
    )
    for _ in range(2):
        await adapter.complete(request_id=uuid4(), locked_request=locked_body())
    async with session_maker() as session:
        rows = (await session.scalars(select(CodingHostedInferenceRequest))).all()
        assert len(rows) == 2
        assert all(
            row.prompt_ceiling == 100 and row.cost_ceiling == 149 for row in rows
        )
        assert all(
            row.prompt_tokens == 20 and row.cost_usd_micros == 11 for row in rows
        )
    assert await adapter.revoke()
    accounting = await ledger.accounting(grant)
    assert accounting.verified and accounting.cost_usd_micros == 22


async def test_real_transport_requires_profile_not_arbitrary_callback(session_maker):
    _, _, policy, _, ledger, grant = await fixture(session_maker)
    with pytest.raises(HostedProviderError):
        HostedProviderAdapter(
            ledger=ledger,
            grant_id=grant,
            policy=policy,
            estimator=SyntheticEstimator(),
            api_key="synthetic",
        )


@pytest.mark.parametrize("value", [0, -1, True, 1.1, 101])
def test_cost_ceiling_requires_valid_completion_bound(value):
    with pytest.raises(ValueError):
        budget_profile().cost_ceiling(value)


async def test_test_seam_cannot_take_real_transport(session_maker):
    _, _, policy, _, ledger, grant = await fixture(session_maker)
    with pytest.raises(HostedProviderError):
        HostedProviderAdapter(
            ledger=ledger,
            grant_id=grant,
            policy=policy,
            estimator=SyntheticEstimator(),
            api_key="synthetic",
            _test_transport=httpx.AsyncBaseTransport(),
        )


async def test_profiled_policy_cannot_use_generic_estimator_even_in_mock(session_maker):
    _, _, policy, _, ledger, grant, _ = await profiled_fixture(session_maker)
    with pytest.raises(HostedProviderError):
        HostedProviderAdapter(
            ledger=ledger,
            grant_id=grant,
            policy=policy,
            estimator=SyntheticEstimator(),
            api_key="synthetic",
            _test_transport=httpx.MockTransport(
                lambda _r: http_response(response_body())
            ),
        )


async def test_profile_expiring_during_commit_never_dispatches(
    session_maker, monkeypatch
):
    now = int(time.time())
    clock = [float(now)]
    profile = budget_profile(valid_from_unix=now - 1, valid_until_unix=now + 2)
    _, _, policy, _, ledger, grant = await fixture(
        session_maker, runtime_profile_sha256=profile.digest()
    )
    estimate = ProfiledBudgetEstimator(
        profile_bytes=profile.canonical_bytes(), policy=policy, now=lambda: clock[0]
    )
    calls = []
    original = ledger.reserve

    async def reserve(**kwargs):
        result = await original(**kwargs)
        clock[0] = profile.valid_until_unix
        return result

    monkeypatch.setattr(ledger, "reserve", reserve)
    adapter = HostedProviderAdapter(
        ledger=ledger,
        grant_id=grant,
        policy=policy,
        estimator=estimate,
        api_key="synthetic",
        _test_transport=httpx.MockTransport(lambda r: calls.append(r)),
    )
    with pytest.raises(HostedProviderError):
        await adapter.complete(request_id=uuid4(), locked_request=locked_body())
    assert not calls
    assert not await adapter.revoke()
    assert (await ledger.accounting(grant)).pending_count == 1


async def test_actual_price_drift_cannot_hide_inside_full_reservation(session_maker):
    _, _, policy, _, ledger, grant, estimate = await profiled_fixture(session_maker)
    payload = response_body()
    payload["usage"]["cost"] = (
        0.000026  # Below reservation 149, above actual-use ceiling 25.
    )
    adapter = HostedProviderAdapter(
        ledger=ledger,
        grant_id=grant,
        policy=policy,
        estimator=estimate,
        api_key="synthetic",
        _test_transport=httpx.MockTransport(lambda _r: http_response(payload)),
    )
    with pytest.raises(HostedProviderError):
        await adapter.complete(request_id=uuid4(), locked_request=locked_body())
    assert not await adapter.revoke()
    state = await ledger.accounting(grant)
    assert state.pending_count == 1 and state.cost_usd_micros is None


async def test_profile_expiring_after_provider_response_keeps_billing_not_output(
    session_maker,
):
    now = int(time.time())
    clock = [float(now)]
    profile = budget_profile(valid_from_unix=now - 1, valid_until_unix=now + 300)
    _, _, policy, _, ledger, grant = await fixture(
        session_maker, runtime_profile_sha256=profile.digest()
    )
    estimate = ProfiledBudgetEstimator(
        profile_bytes=profile.canonical_bytes(), policy=policy, now=lambda: clock[0]
    )

    def provider(_request):
        clock[0] = profile.valid_until_unix
        return http_response(response_body())

    adapter = HostedProviderAdapter(
        ledger=ledger,
        grant_id=grant,
        policy=policy,
        estimator=estimate,
        api_key="synthetic",
        _test_transport=httpx.MockTransport(provider),
    )
    with pytest.raises(HostedProviderError):
        await adapter.complete(request_id=uuid4(), locked_request=locked_body())
    assert await adapter.revoke()
    assert (await ledger.accounting(grant)).cost_usd_micros == 11
