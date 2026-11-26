"""SN-489 end to end: void a 402-tainted canonical score through the operator
endpoint, let the validator re-run it, and watch the median recompute.

Replays the 2026-11-17 shape: one of three validators finished its canonical
run with 1071 of 1473 chat calls refused ``upstream_http_402`` and composite
0.6312 against 0.763616 and 0.809086 from the other two.
"""

from __future__ import annotations

import statistics
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.db.models import (
    Agent,
    AgentStatus,
    InferenceGrant,
    InferenceRequest,
    Score,
    ScoreAuditEntry,
    ValidatorTicket,
)
from ditto.db.queries.audit import (
    EVENT_SCORE_INVALIDATED,
    EVENT_SCORE_RETEST_QUEUED,
)
from ditto.db.queries.score_retests import (
    OUTAGE_VOID_RETEST_BASIS,
    activate_next_score_retest,
)
from ditto.tests.api_server.endpoints.test_validator import (
    _BENCH_VERSION,
    _KEYPAIRS,
    _install_chain,
    _install_db,
    _score_payload,
    _seed_agent,
    _seed_ticket,
)

pytestmark = pytest.mark.asyncio

_TOKEN = "test-admin-token-at-least-32-characters"
_SEED = 8675309
_OUTAGE = _KEYPAIRS[0]
_COMPOSITES = {0: 0.6312, 1: 0.763616, 2: 0.809086}
_CHAT_CALLS = 1473
_REFUSED_402 = 1071


async def _seed_outage_grant(
    maker: async_sessionmaker[AsyncSession], agent_id: UUID, *, refused: int
) -> None:
    created = datetime.now(UTC) - timedelta(minutes=30)
    grant = InferenceGrant(
        grant_id=uuid4(),
        agent_id=agent_id,
        bench_version=_BENCH_VERSION,
        validator_hotkey=_OUTAGE.ss58_address,
        slot_id="slot-0",
        ticket_deadline=created + timedelta(hours=3),
        status="revoked",
        generation=1,
        allowed_models=["openai/gpt-oss-20b"],
        route_provider="openrouter",
        route_profile="openrouter-route-test-v1",
        request_budget=16384,
        token_budget=75_000_000,
        embedding_model="e",
        embedding_profile="openrouter-embedding-test-v1",
        embedding_provider="p",
        embedding_dimensions=768,
        embedding_request_budget=1000,
        embedding_token_budget=1_000_000,
        embedding_request_count=0,
        embedding_tokens=0,
        embedding_cost_microusd=0,
        embedding_active_requests=0,
        request_count=0,
        prompt_tokens=0,
        completion_tokens=0,
        cost_microusd=0,
        active_requests=0,
        expires_at=created + timedelta(hours=3),
        created_at=created,
        updated_at=created,
    )
    async with maker() as session, session.begin():
        session.add(grant)
        await session.flush()
        session.add_all(
            [
                InferenceRequest(
                    grant_id=grant.grant_id,
                    nonce=uuid4(),
                    generation=1,
                    status="failed" if index < refused else "completed",
                    request_kind="chat",
                    model="openai/gpt-oss-20b",
                    reserved_tokens=16,
                    prompt_tokens=0,
                    completion_tokens=0,
                    cost_microusd=0,
                    terminal_error_code=(
                        "upstream_http_402" if index < refused else None
                    ),
                    timed_out=False,
                    started_at=created + timedelta(seconds=index),
                    completed_at=created + timedelta(seconds=index),
                )
                for index in range(_CHAT_CALLS)
            ]
        )


async def _scored_with_outage(
    app: FastAPI,
    client: httpx.AsyncClient,
    maker: async_sessionmaker[AsyncSession],
    *,
    refused: int = _REFUSED_402,
) -> UUID:
    agent_id = await _seed_agent(maker, status=AgentStatus.EVALUATING)
    _install_db(app, maker)
    _install_chain(app)
    for index, keypair in enumerate(_KEYPAIRS):
        await _seed_ticket(maker, agent_id, keypair=keypair)
        if keypair is _OUTAGE:
            await _seed_outage_grant(maker, agent_id, refused=refused)
        response = await client.post(
            f"/api/v1/validator/agent/{agent_id}/score",
            json=_score_payload(
                agent_id,
                run_id=f"canonical_{index}",
                keypair=keypair,
                composite=_COMPOSITES[index],
            ),
        )
        assert response.status_code == 200, response.text
    app.state.config = replace(app.state.config, admin_api_token=_TOKEN)
    return agent_id


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_TOKEN}", "X-Admin-Actor": "operator"}


def _void_body(agent_id: UUID, **overrides: object) -> dict:
    return {
        "items": [
            {
                "agent_id": str(agent_id),
                "validator_hotkey": _OUTAGE.ss58_address,
                "seed": str(_SEED),
                "expected_run_id": "canonical_0",
                "expected_composite": _COMPOSITES[0],
            }
        ],
        "reason": "Nov 17 openrouter 402 outage measured the provider, not the agent",
        **overrides,
    }


async def _median(maker: async_sessionmaker[AsyncSession], agent_id: UUID) -> float:
    async with maker() as session:
        composites = list(
            await session.scalars(
                select(Score.composite).where(Score.agent_id == agent_id)
            )
        )
    assert len(composites) == 3
    return statistics.median(composites)


async def test_void_infra_scores_dry_run_resolves_the_canonical_run(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    agent_id = await _scored_with_outage(app, client, session_maker)

    response = await client.post(
        "/api/v1/admin/confirmation-score-voids",
        headers=_headers(),
        json=_void_body(agent_id),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["dry_run"], body["applied_count"]) == (True, 0)
    (outcome,) = body["outcomes"]
    assert outcome["sample_kind"] == "canonical"
    assert outcome["disposition"] == "ready", outcome["detail"]
    assert outcome["retest_request_id"] is not None
    assert outcome["evidence"]["chat_requests"] == _CHAT_CALLS
    assert outcome["evidence"]["outage_failed_requests"] == _REFUSED_402
    assert outcome["evidence"]["failure_codes"] == {"upstream_http_402": _REFUSED_402}
    async with session_maker() as session:
        queued = await session.scalar(
            select(ScoreAuditEntry.seq).where(
                ScoreAuditEntry.event == EVENT_SCORE_RETEST_QUEUED
            )
        )
    assert queued is None


async def test_clean_canonical_run_refuses_the_whole_batch(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    agent_id = await _scored_with_outage(app, client, session_maker, refused=3)

    response = await client.post(
        "/api/v1/admin/confirmation-score-voids",
        headers=_headers(),
        json=_void_body(agent_id, dry_run=False, confirmation="VOID INFRA SCORES"),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["applied_count"] == 0
    assert body["outcomes"][0]["disposition"] == "insufficient_evidence"


async def test_voided_canonical_score_is_replaced_and_the_median_recomputes(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    agent_id = await _scored_with_outage(app, client, session_maker)
    assert await _median(session_maker, agent_id) == pytest.approx(0.763616)

    applied = await client.post(
        "/api/v1/admin/confirmation-score-voids",
        headers=_headers(),
        json=_void_body(agent_id, dry_run=False, confirmation="VOID INFRA SCORES"),
    )
    assert applied.status_code == 200, applied.text
    (outcome,) = applied.json()["outcomes"]
    assert (outcome["disposition"], applied.json()["applied_count"]) == ("applied", 1)

    # The validator's next free slot promotes the replacement.
    async with session_maker() as session, session.begin():
        before = await session.get(
            ValidatorTicket, (agent_id, _BENCH_VERSION, _OUTAGE.ss58_address)
        )
        assert before is not None
        attempts = before.attempt_count
        ticket = await activate_next_score_retest(
            session,
            validator_hotkey=_OUTAGE.ss58_address,
            now=datetime.now(UTC),
            supports_version=lambda version: version == _BENCH_VERSION,
            required_basis=OUTAGE_VOID_RETEST_BASIS,
            allow_parallel_ordinary=True,
            allow_parallel_contract_retests=True,
        )
        assert ticket is not None
        assert ticket.attempt_count == attempts
        deadline = ticket.deadline

    replacement = await client.post(
        f"/api/v1/validator/agent/{agent_id}/score",
        json=_score_payload(
            agent_id,
            run_id="replacement_0",
            keypair=_OUTAGE,
            composite=0.915097,
            ticket_deadline=deadline,
        ),
    )
    assert replacement.status_code == 200, replacement.text

    async with session_maker() as session:
        swapped = await session.get(
            Score, (agent_id, _BENCH_VERSION, _OUTAGE.ss58_address)
        )
        agent = await session.get(Agent, agent_id)
        invalidated = await session.scalar(
            select(ScoreAuditEntry).where(
                ScoreAuditEntry.agent_id == agent_id,
                ScoreAuditEntry.event == EVENT_SCORE_INVALIDATED,
            )
        )
    assert swapped is not None and swapped.run_id == "replacement_0"
    assert agent is not None and agent.status == AgentStatus.SCORED
    assert invalidated is not None
    assert invalidated.payload["basis"] == OUTAGE_VOID_RETEST_BASIS
    assert invalidated.payload["invalidated_score"]["composite"] == pytest.approx(
        _COMPOSITES[0]
    )
    # The outage run no longer counts: median of 0.763616, 0.809086, 0.915097.
    assert await _median(session_maker, agent_id) == pytest.approx(0.809086)

    # A repeat names a run that is gone.
    repeat = await client.post(
        "/api/v1/admin/confirmation-score-voids",
        headers=_headers(),
        json=_void_body(agent_id),
    )
    assert repeat.json()["outcomes"][0]["disposition"] == "already_replaced"
