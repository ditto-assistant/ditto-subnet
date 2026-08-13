"""Real-Postgres invariants for purpose-bound confirmation inference."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.db.queries.confirmation_inference import (
    ConfirmationInferenceDecline,
    begin_confirmation_inference_request,
    ensure_confirmation_inference_grants,
    finish_confirmation_inference_request,
)
from ditto.tests.confirmation_evidence_fixtures import verification_profile
from ditto.tests.db.queries.test_confirmation_bundles import (
    reserve_and_issue,
    seed_bundle,
)

pytestmark = pytest.mark.asyncio

_NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
_BROKER_KEY = "A" * 43


async def _grants(session: AsyncSession):
    _, revision, settings, bundle = await seed_bundle(session)
    _, ticket = await reserve_and_issue(
        session, bundle=bundle, revision=revision, policy=settings
    )
    offers = await ensure_confirmation_inference_grants(
        session,
        ticket=ticket,
        broker_public_key=_BROKER_KEY,
        profile=verification_profile(),
        now=_NOW,
    )
    return ticket, offers


async def test_grants_are_three_lane_ticket_capabilities_not_stored_bearers(
    session: AsyncSession,
) -> None:
    async with session.begin():
        ticket, offers = await _grants(session)

    assert [grant.lane for grant, _ in offers] == ["embedding", "judge", "reader"]
    assert len({grant.grant_id for grant, _ in offers}) == 3
    assert all(grant.ticket_id == ticket.ticket_id for grant, _ in offers)
    assert all(grant.broker_public_key == _BROKER_KEY for grant, _ in offers)
    assert all(
        grant.bearer_digest == hashlib.sha256(bearer.encode()).hexdigest()
        and bearer not in grant.bearer_digest
        for grant, bearer in offers
    )


async def test_resume_rotates_all_lanes_atomically_without_resetting_spend(
    session: AsyncSession,
) -> None:
    async with session.begin():
        ticket, first = await _grants(session)
        first_by_lane = {
            grant.lane: (grant.grant_id, grant.generation, bearer)
            for grant, bearer in first
        }
        reader, reader_bearer = next(
            (grant, bearer) for grant, bearer in first if grant.lane == "reader"
        )
        admitted = await begin_confirmation_inference_request(
            session,
            grant_id=reader.grant_id,
            nonce=uuid4(),
            bearer=reader_bearer,
            model=reader.model,
            token_reservation=10,
            max_chargeable_tokens=20,
            now=_NOW,
        )
        assert not isinstance(admitted, ConfirmationInferenceDecline)
        # A genuinely live request makes rotation refuse the entire three-lane
        # set; no bearer or generation changes halfway through a resume.
        assert (
            await ensure_confirmation_inference_grants(
                session,
                ticket=ticket,
                broker_public_key="B" * 43,
                profile=verification_profile(),
                now=_NOW + timedelta(seconds=1),
            )
            == []
        )
        assert {
            grant.lane: (grant.grant_id, grant.generation, bearer)
            for grant, bearer in first
        } == first_by_lane

        nonce = admitted[1].nonce
        assert await finish_confirmation_inference_request(
            session,
            grant_id=reader.grant_id,
            nonce=nonce,
            generation=reader.generation,
            status="completed",
            prompt_tokens=7,
            completion_tokens=3,
            cost_microusd=10,
            upstream_provider=reader.receipt_provider,
            now=_NOW + timedelta(seconds=2),
        )
        rotated = await ensure_confirmation_inference_grants(
            session,
            ticket=ticket,
            broker_public_key="B" * 43,
            profile=verification_profile(),
            now=_NOW + timedelta(seconds=3),
        )

    assert len(rotated) == 3
    assert all(
        grant.grant_id == first_by_lane[grant.lane][0]
        and grant.generation == first_by_lane[grant.lane][1] + 1
        and bearer != first_by_lane[grant.lane][2]
        and grant.broker_public_key == "B" * 43
        for grant, bearer in rotated
    )
    rotated_reader = next(grant for grant, _ in rotated if grant.lane == "reader")
    assert rotated_reader.request_count == 1
    assert rotated_reader.prompt_tokens == 7
    assert rotated_reader.completion_tokens == 3


async def test_model_substitution_nonce_replay_and_budget_exhaustion_fail_closed(
    session: AsyncSession,
) -> None:
    async with session.begin():
        _, offers = await _grants(session)
        judge, bearer = next(
            (grant, bearer) for grant, bearer in offers if grant.lane == "judge"
        )
        assert (
            await begin_confirmation_inference_request(
                session,
                grant_id=judge.grant_id,
                nonce=uuid4(),
                bearer=bearer,
                model="forbidden/model",
                token_reservation=1,
                max_chargeable_tokens=1,
                now=_NOW,
            )
            is ConfirmationInferenceDecline.MODEL_NOT_PERMITTED
        )
        nonce = uuid4()
        admitted = await begin_confirmation_inference_request(
            session,
            grant_id=judge.grant_id,
            nonce=nonce,
            bearer=bearer,
            model=judge.model,
            token_reservation=1,
            max_chargeable_tokens=2,
            now=_NOW,
        )
        assert not isinstance(admitted, ConfirmationInferenceDecline)
        judge.request_budget = 1
        assert (
            await begin_confirmation_inference_request(
                session,
                grant_id=judge.grant_id,
                nonce=nonce,
                bearer=bearer,
                model=judge.model,
                token_reservation=1,
                max_chargeable_tokens=2,
                now=_NOW,
            )
            is ConfirmationInferenceDecline.NONCE_REPLAYED
        )
        assert not await finish_confirmation_inference_request(
            session,
            grant_id=judge.grant_id,
            nonce=nonce,
            generation=judge.generation,
            status="completed",
            prompt_tokens=1,
            completion_tokens=0,
            cost_microusd=1,
            upstream_provider="wrong-provider",
            now=_NOW + timedelta(seconds=1),
        )
        assert judge.prompt_tokens == 1
        assert judge.active_requests == 0
        assert (
            await begin_confirmation_inference_request(
                session,
                grant_id=judge.grant_id,
                nonce=uuid4(),
                bearer=bearer,
                model=judge.model,
                token_reservation=1,
                max_chargeable_tokens=2,
                now=_NOW + timedelta(seconds=2),
            )
            is ConfirmationInferenceDecline.BUDGET_EXHAUSTED
        )
