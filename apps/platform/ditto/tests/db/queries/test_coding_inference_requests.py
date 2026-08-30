from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy import Table
from sqlalchemy.exc import IntegrityError

from ditto.api_models.coding_inference import (
    CodingInferencePolicy,
    CodingInferenceProviderSettlement,
    policy_digest,
)
from ditto.db.models import CodingInferenceGrant, CodingInferenceRequest
from ditto.db.queries.coding_inference_grants import (
    coding_inference_bearer_digest,
)
from ditto.db.queries.coding_inference_requests import (
    CodingInferenceDispatchAuthority,
    CodingInferenceRequestConflictError,
    CodingInferenceRequestIntegrityError,
    CodingInferenceRequestNotAvailableError,
    begin_coding_inference_request,
    fail_coding_inference_request_unsettled,
    settle_coding_inference_request,
)

_ROOT = Path(__file__).parents[6]
_POLICY_PATH = (
    _ROOT
    / "packages/dittobench-coding-contract/testdata/coding_inference_policy_v1.json"
)
_NOW = datetime(2026, 8, 22, 20, tzinfo=UTC)
_BEARER = "private-coding-bearer-0000000000000000000000"


def _vector() -> tuple[
    CodingInferencePolicy,
    dict[str, list[CodingInferenceProviderSettlement]],
]:
    vector = json.loads(_POLICY_PATH.read_text(encoding="utf-8"))
    return (
        CodingInferencePolicy.model_validate(vector["policy"]),
        {
            name: [
                CodingInferenceProviderSettlement.model_validate_json(json.dumps(item))
                for item in items
            ]
            for name, items in vector["provider_settlements"].items()
        },
    )


def _grant(
    policy: CodingInferencePolicy,
    settlement: CodingInferenceProviderSettlement,
) -> CodingInferenceGrant:
    return CodingInferenceGrant(
        grant_id=settlement.grant_id,
        ticket_id=settlement.ticket_id,
        run_row_id=uuid4(),
        task_count=1,
        validator_hotkey="5" + "V" * 47,
        case_id=settlement.case_id,
        profile_capability_id=settlement.profile_capability_id,
        inference_grant_sha256=policy_digest(policy),
        model=policy.model,
        provider_api=policy.provider_api,
        provider_route=policy.provider_route,
        receipt_provider=policy.receipt_provider,
        provider_route_profile=policy.provider_route_profile,
        provider_account_guardrail=policy.provider_account_guardrail,
        provider_pipeline_policy=policy.provider_pipeline_policy,
        provider_cache_policy=policy.provider_cache_policy,
        reasoning_effort=policy.reasoning_effort,
        status="active",
        bearer_digest=coding_inference_bearer_digest(_BEARER),
        revoke_bearer_digest="dd" * 32,
        broker_public_key="A" * 43,
        generation=settlement.generation,
        request_budget=166,
        prompt_token_budget=200_000,
        completion_token_budget=30_000,
        cost_budget_usd_micros=policy.max_cost_usd_micros,
        request_count=0,
        prompt_tokens=0,
        completion_tokens=0,
        cost_usd_micros=0,
        active_requests=0,
        expires_at=_NOW + timedelta(hours=1),
        revoked_at=None,
        weight_eligible=False,
        created_at=_NOW - timedelta(minutes=1),
        updated_at=_NOW - timedelta(minutes=1),
    )


def _authority(
    settlement: CodingInferenceProviderSettlement,
    *,
    sequence: int,
) -> CodingInferenceDispatchAuthority:
    return CodingInferenceDispatchAuthority(
        grant_id=settlement.grant_id,
        ticket_id=settlement.ticket_id,
        generation=settlement.generation,
        sequence=sequence,
        request_sequence=settlement.request_sequence,
        attempt=settlement.attempt,
        request_id=settlement.request_id,
        case_id=settlement.case_id,
        profile_capability_id=settlement.profile_capability_id,
        inference_grant_sha256=settlement.inference_grant_sha256,
        locked_request_sha256=settlement.locked_request_sha256,
    )


class _Session:
    def __init__(self, scalars: list[object]) -> None:
        self.scalars = list(scalars)
        self.added: list[CodingInferenceRequest] = []
        self.flushes = 0

    async def scalar(self, statement):
        del statement
        if not self.scalars:
            raise AssertionError("unexpected scalar query")
        return self.scalars.pop(0)

    def add(self, value: CodingInferenceRequest) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        self.flushes += 1


async def _begin(
    grant: CodingInferenceGrant,
    authority: CodingInferenceDispatchAuthority,
    *,
    latest: CodingInferenceRequest | None = None,
) -> CodingInferenceRequest:
    session = _Session([grant, latest, _NOW])
    result = await begin_coding_inference_request(
        session,  # type: ignore[arg-type]
        authority=authority,
        bearer=_BEARER,
    )
    assert result.idempotent is False
    assert session.scalars == [] and session.flushes == 1
    assert session.added == [result.request]
    return result.request


async def test_begin_and_complete_persist_only_bounded_canonical_settlement() -> None:
    policy, settlements = _vector()
    complete = settlements["complete"][0]
    grant = _grant(policy, complete)
    authority = _authority(complete, sequence=1)
    request = await _begin(grant, authority)

    assert request.status == "started"
    assert request.provider_settlement_json is None
    assert grant.request_count == 1 and grant.active_requests == 1
    assert _BEARER not in repr(request.__dict__)

    session = _Session([grant, request, _NOW + timedelta(seconds=1), None])
    result = await settle_coding_inference_request(
        session,  # type: ignore[arg-type]
        authority=authority,
        settlement=complete,
        policy=policy,
    )
    assert result.idempotent is False
    assert request.status == "complete"
    assert request.provider_settlement_json is not None
    assert request.provider_generation_id == complete.provider_generation_id
    assert len(request.provider_settlement_json.encode()) < 65_536
    assert _BEARER not in request.provider_settlement_json
    assert grant.active_requests == 0
    assert grant.prompt_tokens == complete.prompt_tokens
    assert grant.completion_tokens == complete.completion_tokens
    assert grant.cost_usd_micros == complete.cost_usd_micros

    grant.generation += 1
    grant.request_count += 1
    replay = await settle_coding_inference_request(
        _Session([grant, request, _NOW + timedelta(seconds=2)]),  # type: ignore[arg-type]
        authority=authority,
        settlement=complete,
        policy=policy,
    )
    assert replay.idempotent is True

    request.provider_settlement_json = "{}"
    with pytest.raises(CodingInferenceRequestIntegrityError):
        await settle_coding_inference_request(
            _Session([grant, request, _NOW + timedelta(seconds=3)]),  # type: ignore[arg-type]
            authority=authority,
            settlement=complete,
            policy=policy,
        )


async def test_inflight_begin_replay_and_drift_guards() -> None:
    policy, settlements = _vector()
    complete = settlements["complete"][0]
    grant = _grant(policy, complete)
    authority = _authority(complete, sequence=1)
    request = await _begin(grant, authority)

    replay = await begin_coding_inference_request(
        _Session([grant, request, _NOW]),  # type: ignore[arg-type]
        authority=authority,
        bearer=_BEARER,
    )
    assert replay.idempotent is True and replay.request is request
    assert grant.request_count == 1

    with pytest.raises(CodingInferenceRequestConflictError):
        await begin_coding_inference_request(
            _Session([grant, request, _NOW]),  # type: ignore[arg-type]
            authority=replace(authority, locked_request_sha256="ab" * 32),
            bearer=_BEARER,
        )
    request.status = "complete"
    with pytest.raises(CodingInferenceRequestConflictError):
        await begin_coding_inference_request(
            _Session([grant, request, _NOW]),  # type: ignore[arg-type]
            authority=authority,
            bearer=_BEARER,
        )


async def test_receipt_free_retry_reuses_identity_without_double_counting_request() -> (
    None
):
    policy, settlements = _vector()
    retry, complete = settlements["retry_complete"]
    grant = _grant(policy, retry)
    grant.request_budget = 1
    first_authority = _authority(retry, sequence=1)
    first = await _begin(grant, first_authority)
    await settle_coding_inference_request(
        _Session([grant, first, _NOW + timedelta(seconds=1), None]),  # type: ignore[arg-type]
        authority=first_authority,
        settlement=retry,
        policy=policy,
    )
    assert first.status == "receipt_free_retry"
    assert first.provider_generation_id is None
    assert grant.status == "active" and grant.request_count == 1

    second_authority = _authority(complete, sequence=2)
    second = await _begin(grant, second_authority, latest=first)
    assert second.request_id == first.request_id
    assert second.attempt == 2
    assert grant.request_count == 1
    await settle_coding_inference_request(
        _Session([grant, second, _NOW + timedelta(seconds=2), None]),  # type: ignore[arg-type]
        authority=second_authority,
        settlement=complete,
        policy=policy,
    )
    assert second.status == "complete"
    assert grant.prompt_tokens == complete.prompt_tokens


async def test_settlement_replay_is_exact_after_budget_exhaustion() -> None:
    policy, settlements = _vector()
    complete = settlements["complete"][0]
    grant = _grant(policy, complete)
    grant.request_budget = 1
    authority = _authority(complete, sequence=1)
    request = await _begin(grant, authority)
    first = await settle_coding_inference_request(
        _Session([grant, request, _NOW + timedelta(seconds=1), None]),  # type: ignore[arg-type]
        authority=authority,
        settlement=complete,
        policy=policy,
    )
    assert first.idempotent is False
    assert grant.status == "exhausted"
    assert grant.bearer_digest is None and grant.broker_public_key is None

    replay = await settle_coding_inference_request(
        _Session([grant, request, _NOW + timedelta(seconds=2)]),  # type: ignore[arg-type]
        authority=authority,
        settlement=complete,
        policy=policy,
    )
    assert replay.idempotent is True
    assert grant.prompt_tokens == complete.prompt_tokens


async def test_real_spend_is_booked_even_when_it_crosses_remaining_budget() -> None:
    policy, settlements = _vector()
    complete = settlements["complete"][0]
    grant = _grant(policy, complete)
    grant.prompt_token_budget = complete.prompt_tokens
    grant.completion_token_budget = complete.completion_tokens
    grant.cost_budget_usd_micros = complete.cost_usd_micros
    authority = _authority(complete, sequence=1)
    request = await _begin(grant, authority)
    await settle_coding_inference_request(
        _Session([grant, request, _NOW + timedelta(seconds=1), None]),  # type: ignore[arg-type]
        authority=authority,
        settlement=complete,
        policy=policy,
    )
    assert grant.status == "exhausted"
    assert (
        grant.prompt_tokens,
        grant.completion_tokens,
        grant.cost_usd_micros,
    ) == (
        complete.prompt_tokens,
        complete.completion_tokens,
        complete.cost_usd_micros,
    )


async def test_unaccountable_bigint_settlement_is_unsettled_and_revokes() -> None:
    policy, settlements = _vector()
    complete = settlements["complete"][0]
    grant = _grant(policy, complete)
    authority = _authority(complete, sequence=1)
    request = await _begin(grant, authority)
    grant.prompt_tokens = (1 << 63) - 1
    result = await settle_coding_inference_request(
        _Session([grant, request, _NOW + timedelta(seconds=1), None]),  # type: ignore[arg-type]
        authority=authority,
        settlement=complete,
        policy=policy,
    )
    assert result.idempotent is False
    assert request.status == "unsettled"
    assert request.unsettled_reason == "invalid_provider_settlement"
    assert request.provider_settlement_json is None
    assert grant.status == "revoked"
    assert grant.prompt_tokens == (1 << 63) - 1
    assert grant.active_requests == 0


async def test_concurrent_settlement_identity_reuse_is_integrity_error() -> None:
    policy, settlements = _vector()
    complete = settlements["complete"][0]
    grant = _grant(policy, complete)
    authority = _authority(complete, sequence=1)
    request = await _begin(grant, authority)

    class _ConflictSession(_Session):
        async def flush(self) -> None:
            raise IntegrityError("UNIQUE", {}, Exception("duplicate"))

    with pytest.raises(
        CodingInferenceRequestIntegrityError, match="identity was reused"
    ):
        await settle_coding_inference_request(
            _ConflictSession([grant, request, _NOW + timedelta(seconds=1), None]),  # type: ignore[arg-type]
            authority=authority,
            settlement=complete,
            policy=policy,
        )


async def test_provider_failure_is_durably_accounted_and_revokes_grant() -> None:
    policy, settlements = _vector()
    failure = settlements["provider_failure"][0]
    grant = _grant(policy, failure)
    authority = _authority(failure, sequence=1)
    request = await _begin(grant, authority)

    await settle_coding_inference_request(
        _Session([grant, request, _NOW + timedelta(seconds=1), None]),  # type: ignore[arg-type]
        authority=authority,
        settlement=failure,
        policy=policy,
    )
    assert request.status == "provider_failure"
    assert request.provider_settlement_sha256 is not None
    assert grant.status == "revoked"
    assert grant.bearer_digest is None and grant.active_requests == 0
    assert grant.prompt_tokens == failure.prompt_tokens
    assert grant.completion_tokens == failure.completion_tokens
    assert grant.cost_usd_micros == failure.cost_usd_micros


async def test_provider_settlement_identity_cannot_be_reused() -> None:
    policy, settlements = _vector()
    complete = settlements["complete"][0]
    grant = _grant(policy, complete)
    authority = _authority(complete, sequence=1)
    request = await _begin(grant, authority)

    with pytest.raises(CodingInferenceRequestIntegrityError):
        await settle_coding_inference_request(
            _Session([grant, request, _NOW + timedelta(seconds=1), uuid4()]),  # type: ignore[arg-type]
            authority=authority,
            settlement=complete,
            policy=policy,
        )
    assert request.status == "started"
    assert request.provider_generation_id is None
    assert grant.active_requests == 1 and grant.prompt_tokens == 0


async def test_unsettled_provider_activity_revokes_without_clean_retry() -> None:
    policy, settlements = _vector()
    complete = settlements["complete"][0]
    grant = _grant(policy, complete)
    authority = _authority(complete, sequence=1)
    request = await _begin(grant, authority)

    failed = await fail_coding_inference_request_unsettled(
        _Session([grant, request, _NOW + timedelta(seconds=1)]),  # type: ignore[arg-type]
        authority=authority,
        reason="provider_response_lost",
    )
    assert failed.idempotent is False
    assert request.status == "unsettled"
    assert request.provider_settlement_json is None
    assert grant.status == "revoked" and grant.active_requests == 0
    assert grant.bearer_digest is None

    replay = await fail_coding_inference_request_unsettled(
        _Session([grant, request, _NOW + timedelta(seconds=2)]),  # type: ignore[arg-type]
        authority=authority,
        reason="provider_response_lost",
    )
    assert replay.idempotent is True
    with pytest.raises(CodingInferenceRequestConflictError):
        await fail_coding_inference_request_unsettled(
            _Session([grant, request, _NOW + timedelta(seconds=3)]),  # type: ignore[arg-type]
            authority=authority,
            reason="relay_infrastructure",
        )


async def test_wrong_bearer_and_authority_fail_before_reservation() -> None:
    policy, settlements = _vector()
    complete = settlements["complete"][0]
    grant = _grant(policy, complete)
    authority = _authority(complete, sequence=1)
    session = _Session([grant, None, _NOW])
    with pytest.raises(CodingInferenceRequestNotAvailableError):
        await begin_coding_inference_request(
            session,  # type: ignore[arg-type]
            authority=authority,
            bearer="wrong",
        )
    assert session.added == [] and grant.request_count == 0

    with pytest.raises(CodingInferenceRequestIntegrityError):
        await begin_coding_inference_request(
            _Session([]),  # type: ignore[arg-type]
            authority=replace(authority, case_id="bad case"),
            bearer=_BEARER,
        )
    with pytest.raises(CodingInferenceRequestIntegrityError):
        await begin_coding_inference_request(
            _Session([]),  # type: ignore[arg-type]
            authority=replace(authority, case_id="é" * 200),
            bearer=_BEARER,
        )


async def test_settlement_identity_drift_does_not_mutate_started_request() -> None:
    policy, settlements = _vector()
    complete = settlements["complete"][0]
    grant = _grant(policy, complete)
    authority = _authority(complete, sequence=1)
    request = await _begin(grant, authority)
    drifted = complete.model_copy(update={"case_id": "different-case"})
    with pytest.raises(CodingInferenceRequestIntegrityError):
        await settle_coding_inference_request(
            _Session([]),  # type: ignore[arg-type]
            authority=authority,
            settlement=drifted,
            policy=policy,
        )
    assert request.status == "started"
    assert grant.prompt_tokens == 0 and grant.active_requests == 1


def test_model_declares_secret_free_bounded_state_machine() -> None:
    table = cast(Table, CodingInferenceRequest.__table__)
    columns = table.columns
    assert "bearer" not in columns
    assert "prompt" not in columns
    assert "raw_response" not in columns
    assert "provider_credential" not in columns
    checks = " ".join(
        str(constraint.sqltext)
        for constraint in table.constraints
        if hasattr(constraint, "sqltext")
    )
    assert "weight_eligible = false" in checks
    assert "provider_settlement_json" in checks
    assert "unsettled" in checks
