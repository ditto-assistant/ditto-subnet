from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import delete, event, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.coding_canonical import coding_canonical_json_bytes
from ditto.api_models.coding_hosted_inference import (
    HostedInferencePolicy,
    HostedInferenceSettlement,
)
from ditto.api_models.coding_inference import CodingInferencePolicy
from ditto.api_server.coding_hosted_inference import (
    HostedInferenceError,
    HostedInferenceLedger,
    ReservationCeilings,
)
from ditto.db.models import CodingHostedInferenceGrant, CodingHostedInferenceRequest
from ditto.tests.db.queries.test_coding_hosted_private import _close, _freeze, _prepared

ROOT = Path(__file__).resolve().parents[5]


def native_policy(**changes):
    source = json.loads(
        (
            ROOT
            / "packages/dittobench-coding-contract/testdata"
            / "coding_inference_policy_locked_v1.json"
        ).read_bytes()
    )
    source.update(
        schema="dittobench-coding-hosted-inference-policy-v2",
        retry_policy="no_retries_v2",
        max_requests=4,
        max_prompt_tokens=1000,
        max_completion_tokens=1000,
        max_completion_tokens_per_request=100,
        max_cost_usd_micros=1000,
    )
    source.update(changes)
    return HostedInferencePolicy.model_validate(source)


def locked_body():
    source = json.loads(
        (
            ROOT
            / "packages/dittobench-coding-contract/testdata"
            / "coding_inference_policy_v1.json"
        ).read_bytes()
    )["locked_requests"][0]
    source["max_completion_tokens"] = 100
    return json.dumps(source).encode()


async def fixture(maker, **policy_changes):
    policy = native_policy(**policy_changes)
    # This tests budget authority, not executor image/profile certification.
    profile = coding_canonical_json_bytes(
        {
            "schema": "dittobench-coding-hosted-authoring-profile-v2",
            "budgets": {
                "model_input_tokens": 200,
                "model_output_tokens": 200,
                "workspace_tool_calls": 5,
                "wall_time_seconds": 600,
            },
        },
        maximum_bytes=16384,
        label="synthetic profile",
    )
    authority, _, worker, _ = await _prepared(
        maker,
        policy_sha256=policy.digest(),
        execution_profile_sha256=hashlib.sha256(profile).hexdigest(),
    )
    ledger = HostedInferenceLedger(sessions=maker, worker_id=worker)
    grant = await ledger.issue(
        evaluation_id=authority.evaluation_id,
        attempt_id=authority.attempt_id,
        assignment_sha256=authority.digest(),
        policy=policy,
        execution_profile=profile,
    )
    return authority, worker, policy, profile, ledger, grant


def settlement(authority, policy, reservation, **changes):
    source = {
        "schema": "dittobench-coding-hosted-inference-settlement-v2",
        "evaluation_id": str(authority.evaluation_id),
        "attempt_id": str(authority.attempt_id),
        "grant_id": str(reservation.grant_id),
        "request_id": str(reservation.request_id),
        "sequence": reservation.sequence,
        "policy_sha256": policy.digest(),
        "locked_request_sha256": reservation.locked_request_sha256,
        "response_sha256": "d" * 64,
        "provider_receipt_sha256": hashlib.sha256(
            str(reservation.request_id).encode()
        ).hexdigest(),
        "model": policy.model,
        "provider": policy.receipt_provider,
        "provider_route": policy.provider_route,
        "provider_route_profile": policy.provider_route_profile,
        "fallback_used": False,
        "prompt_tokens": 20,
        "completion_tokens": 10,
        "cost_usd_micros": 10,
    }
    source.update(changes)
    return HostedInferenceSettlement.model_validate(source)


async def reserve(ledger, grant, request_id=None, **limits):
    values = {"prompt_tokens": 100, "completion_tokens": 100, "cost_usd_micros": 100}
    values.update(limits)
    return await ledger.reserve(
        grant_id=grant,
        request_id=request_id or uuid4(),
        locked_request=locked_body(),
        ceilings=ReservationCeilings(**values),
    )


async def test_native_grant_is_bound_and_replay_never_dispatches(session_maker):
    authority, worker, policy, profile, ledger, grant = await fixture(session_maker)
    assert (
        await ledger.issue(
            evaluation_id=authority.evaluation_id,
            attempt_id=authority.attempt_id,
            assignment_sha256=authority.digest(),
            policy=policy,
            execution_profile=profile,
        )
        == grant
    )
    request_id = uuid4()
    attempts = await asyncio.gather(
        *(reserve(ledger, grant, request_id) for _ in range(8))
    )
    assert sum(item.newly_reserved for item in attempts) == 1
    assert {item.sequence for item in attempts} == {1}
    async with session_maker() as session:
        row = await session.get(CodingHostedInferenceGrant, grant)
        assert row.worker_id == worker and row.weight_eligible is False
        assert row.prompt_limit == 200 and row.completion_limit == 200
        assert (
            len((await session.scalars(select(CodingHostedInferenceRequest))).all())
            == 1
        )
    other = HostedInferenceLedger(sessions=session_maker, worker_id=uuid4())
    with pytest.raises(ValueError):
        await reserve(other, grant)
    with pytest.raises(HostedInferenceError):
        await reserve(ledger, grant)


async def test_settlement_releases_only_unused_reservation_and_freeze_requires_drain(
    session_maker,
):
    authority, worker, policy, _, ledger, grant = await fixture(session_maker)
    first = await reserve(ledger, grant)
    with pytest.raises(IntegrityError):
        await _freeze(session_maker, authority, worker)
    assert not await ledger.revoke(grant)
    with pytest.raises(IntegrityError):
        await _freeze(session_maker, authority, worker)
    value = settlement(authority, policy, first)
    assert await ledger.settle(value)
    assert not await ledger.settle(value)
    assert await ledger.revoke(grant)
    accounting = await ledger.accounting(grant)
    assert accounting.verified and accounting.prompt_tokens == 20
    assert (await _freeze(session_maker, authority, worker)).newly_frozen
    with pytest.raises(HostedInferenceError):
        await reserve(ledger, grant)


async def test_usage_cost_and_request_caps_are_enforced(session_maker):
    authority, _, policy, _, ledger, grant = await fixture(
        session_maker, max_requests=2, max_cost_usd_micros=150
    )
    first = await reserve(ledger, grant)
    with pytest.raises(HostedInferenceError):
        await ledger.settle(settlement(authority, policy, first, prompt_tokens=101))
    assert await ledger.settle(
        settlement(
            authority,
            policy,
            first,
            prompt_tokens=100,
            completion_tokens=100,
            cost_usd_micros=100,
        )
    )
    with pytest.raises(HostedInferenceError):
        await reserve(ledger, grant, cost_usd_micros=51)
    second = await reserve(ledger, grant, cost_usd_micros=50)
    assert second.sequence == 2
    assert await ledger.settle(
        settlement(
            authority,
            policy,
            second,
            prompt_tokens=100,
            completion_tokens=100,
            cost_usd_micros=50,
        )
    )
    with pytest.raises(HostedInferenceError):
        await reserve(ledger, grant, prompt_tokens=1, cost_usd_micros=1)


async def test_unknown_outcome_keeps_reservations_and_revokes(session_maker):
    authority, worker, policy, _, ledger, grant = await fixture(session_maker)
    first = await reserve(ledger, grant)
    await ledger.mark_uncertain(grant_id=grant, request_id=first.request_id)
    await ledger.mark_uncertain(grant_id=grant, request_id=first.request_id)
    accounting = await ledger.accounting(grant)
    assert not accounting.verified and accounting.uncertain_count == 1
    assert accounting.prompt_tokens is None and accounting.cost_usd_micros is None
    with pytest.raises(HostedInferenceError):
        await ledger.settle(settlement(authority, policy, first))
    with pytest.raises(HostedInferenceError):
        await reserve(ledger, grant)
    async with session_maker() as session:
        row = await session.get(CodingHostedInferenceRequest, first.request_id)
        assert (
            row.state == "uncertain"
            and row.prompt_tokens is None
            and row.prompt_ceiling == 100
        )
        assert (
            await session.get(CodingHostedInferenceGrant, grant)
        ).revoked_at is not None
    assert await ledger.revoke(grant)
    assert (await _freeze(session_maker, authority, worker)).newly_frozen


async def test_close_revokes_and_cannot_reopen_or_change_authority(session_maker):
    authority, worker, policy, profile, ledger, grant = await fixture(session_maker)
    assert await _close(session_maker, authority, worker)
    with pytest.raises(ValueError):
        await ledger.issue(
            evaluation_id=authority.evaluation_id,
            attempt_id=authority.attempt_id,
            assignment_sha256=authority.digest(),
            policy=policy,
            execution_profile=profile,
        )
    with pytest.raises(HostedInferenceError):
        await reserve(ledger, grant)
    for statement in (
        update(CodingHostedInferenceGrant).values(revoked_at=None),
        update(CodingHostedInferenceGrant).values(prompt_limit=1000),
        delete(CodingHostedInferenceGrant),
    ):
        with pytest.raises(IntegrityError):
            async with session_maker() as session, session.begin():
                await session.execute(statement)


async def test_request_row_guards_block_budget_bypass_and_rewriting(session_maker):
    authority, _, policy, _, ledger, grant = await fixture(session_maker)
    first = await reserve(ledger, grant)
    with pytest.raises(IntegrityError):
        async with session_maker() as session, session.begin():
            session.add(
                CodingHostedInferenceRequest(
                    request_id=uuid4(),
                    grant_id=grant,
                    sequence=2,
                    locked_request_sha256="a" * 64,
                    prompt_ceiling=1,
                    completion_ceiling=1,
                    cost_ceiling=1,
                    state="reserved",
                )
            )
    with pytest.raises(IntegrityError):
        async with session_maker() as session, session.begin():
            await session.execute(
                update(CodingHostedInferenceRequest).values(prompt_ceiling=1)
            )
    assert await ledger.settle(settlement(authority, policy, first))
    with pytest.raises(IntegrityError):
        async with session_maker() as session, session.begin():
            await session.execute(
                update(CodingHostedInferenceRequest).values(prompt_tokens=0)
            )


def test_v2_policy_is_locked_and_legacy_parser_stays_v1():
    policy = native_policy()
    policy.locked_request(locked_body())
    with pytest.raises(ValueError):
        CodingInferencePolicy.model_validate(
            policy.model_dump(mode="json", by_alias=True)
        )
    for field, value in (
        ("allow_fallbacks", True),
        ("zdr", False),
        ("provider_route", "other"),
        ("model", "other"),
        ("stream", 1),
    ):
        with pytest.raises(ValueError):
            native_policy(**{field: value})
    request = json.loads(locked_body())
    request["provider"]["allow_fallbacks"] = True
    with pytest.raises(ValueError):
        policy.locked_request(json.dumps(request).encode())


async def test_unused_reservations_are_released_but_receipts_cannot_be_reused(
    session_maker,
):
    authority, _, policy, _, ledger, grant = await fixture(session_maker)
    first = await reserve(ledger, grant, prompt_tokens=190)
    value = settlement(authority, policy, first)
    assert await ledger.settle(value)
    second = await reserve(ledger, grant, prompt_tokens=180)
    assert second.newly_reserved
    with pytest.raises(HostedInferenceError):
        await ledger.settle(
            settlement(authority, policy, second, policy_sha256="f" * 64)
        )
    with pytest.raises(IntegrityError):
        await ledger.settle(
            settlement(
                authority,
                policy,
                second,
                provider_receipt_sha256=value.provider_receipt_sha256,
            )
        )
    assert await ledger.settle(settlement(authority, policy, second))


async def test_failed_reservation_commit_never_returns_permission(
    engine, session_maker
):
    _, worker, _, _, ledger, grant = await fixture(session_maker)

    class FailedCommit(AsyncSession):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            event.listen(self.sync_session, "before_commit", self.fail)

        @staticmethod
        def fail(_session):
            raise RuntimeError("synthetic failed commit")

    faulty = HostedInferenceLedger(
        sessions=async_sessionmaker(
            engine, class_=FailedCommit, expire_on_commit=False
        ),
        worker_id=worker,
    )
    request_id = uuid4()
    with pytest.raises(RuntimeError):
        await reserve(faulty, grant, request_id)
    async with session_maker() as session:
        assert await session.get(CodingHostedInferenceRequest, request_id) is None
    assert (await reserve(ledger, grant, request_id)).newly_reserved


async def test_revocation_cannot_be_bypassed_by_a_concurrent_reservation(session_maker):
    _, _, _, _, ledger, grant = await fixture(session_maker)
    async with session_maker() as owner, owner.begin():
        row = await owner.get(CodingHostedInferenceGrant, grant, with_for_update=True)
        pending = asyncio.create_task(reserve(ledger, grant))
        await asyncio.sleep(0.05)
        from ditto.db.queries.coding_hosted_admission import _now

        row.revoked_at = await _now(owner)
    with pytest.raises(HostedInferenceError):
        await pending
    async with session_maker() as session:
        assert (
            list((await session.scalars(select(CodingHostedInferenceRequest))).all())
            == []
        )


async def test_null_settlement_fields_do_not_satisfy_terminal_state(session_maker):
    _, _, _, _, ledger, grant = await fixture(session_maker)
    first = await reserve(ledger, grant)
    with pytest.raises(IntegrityError):
        async with session_maker() as session, session.begin():
            await session.execute(
                update(CodingHostedInferenceRequest)
                .where(CodingHostedInferenceRequest.request_id == first.request_id)
                .values(state="settled")
            )
