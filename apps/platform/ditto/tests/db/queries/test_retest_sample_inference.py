"""Each confirmation sample is paired with the grant that served it.

The fixture is the 2026-10-31 Wonder v6 shape: an earlier canonical grant that
completed cleanly and a later retest grant on the same validator whose chat
calls the gateway refused for exhausted credits. The low sample must be read
against the retest grant, never the clean canonical one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.ticket_status import TicketStatus
from ditto.db.models import (
    Agent,
    AgentStatus,
    ConfirmationScore,
    InferenceGrant,
    InferenceRequest,
    ValidatorTicket,
)
from ditto.db.queries.retest_sample_inference import retest_sample_inference

pytestmark = pytest.mark.asyncio

_BENCH_VERSION = 14
_NOW = datetime(2026, 10, 31, 4, 20, tzinfo=UTC)
_VALIDATOR = "5HEouuDaDASnWdqDwYtwQATS5z916W9DyDAtK6WyB2jPRReb"
_OTHER = "5CFtzzb4vym9eysfeF9cxxp6D7gksuUVTKYNq1mchnrMs118"


def _grant(agent_id: UUID, validator: str, *, created_at: datetime) -> InferenceGrant:
    return InferenceGrant(
        grant_id=uuid4(),
        agent_id=agent_id,
        bench_version=_BENCH_VERSION,
        validator_hotkey=validator,
        slot_id="slot-0",
        ticket_deadline=created_at + timedelta(hours=3),
        status="revoked",
        generation=1,
        allowed_models=["openai/gpt-oss-20b"],
        route_provider="openrouter",
        route_profile="openrouter-route-test-v1",
        request_budget=16384,
        token_budget=75_000_000,
        embedding_model="test-embedding",
        embedding_profile="openrouter-embedding-test-v1",
        embedding_provider="test-provider",
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
        expires_at=created_at + timedelta(hours=3),
        created_at=created_at,
        updated_at=created_at,
    )


def _requests(
    grant: InferenceGrant,
    *,
    status: str,
    count: int,
    code: str | None = None,
    kind: str = "chat",
) -> list[InferenceRequest]:
    return [
        InferenceRequest(
            grant_id=grant.grant_id,
            nonce=uuid4(),
            generation=1,
            status=status,
            request_kind=kind,
            model="openai/gpt-oss-20b",
            reserved_tokens=16,
            prompt_tokens=0,
            completion_tokens=0,
            cost_microusd=0,
            terminal_error_code=code,
            timed_out=False,
            started_at=grant.created_at,
            completed_at=grant.created_at,
        )
        for _ in range(count)
    ]


async def test_low_sample_reads_its_own_retest_grant(session: AsyncSession) -> None:
    agent = Agent(
        agent_id=uuid4(),
        miner_hotkey="5HTUbRK5bN7md878oMKByDYp5chz2STqoyW8uwGG9c1vZdLy",
        name="wonder",
        sha256=uuid4().hex * 2,
        status=AgentStatus.SCORED,
        created_at=_NOW - timedelta(hours=4),
    )
    tickets = [
        ValidatorTicket(
            agent_id=agent.agent_id,
            validator_hotkey=validator,
            slot_id="slot-0",
            status=TicketStatus.SCORED,
            issued_at=_NOW - timedelta(hours=3),
            deadline=_NOW + timedelta(hours=2),
            bench_version=_BENCH_VERSION,
            attempt_count=2,
        )
        for validator in (_VALIDATOR, _OTHER)
    ]
    canonical = _grant(agent.agent_id, _VALIDATOR, created_at=_NOW - timedelta(hours=3))
    retest = _grant(agent.agent_id, _VALIDATOR, created_at=_NOW - timedelta(minutes=30))
    healthy = _grant(agent.agent_id, _OTHER, created_at=_NOW - timedelta(minutes=12))
    async with session.begin():
        session.add_all([agent, *tickets])
        await session.flush()
        session.add_all([canonical, retest, healthy])
        await session.flush()
        session.add_all(
            [
                *_requests(canonical, status="completed", count=5),
                *_requests(retest, status="completed", count=4),
                *_requests(retest, status="failed", count=5, code="upstream_http_402"),
                *_requests(retest, status="failed", count=1, code="provider_timeout"),
                # Embedding failures are a different lane and not counted.
                *_requests(
                    retest, status="failed", count=7, code="x", kind="embedding"
                ),
                *_requests(healthy, status="completed", count=3),
                ConfirmationScore(
                    agent_id=agent.agent_id,
                    validator_hotkey=_VALIDATOR,
                    bench_version=_BENCH_VERSION,
                    seed=4_656_752_738_245_642_784,
                    composite=0.651813,
                    run_id="fd227604",
                    created_at=_NOW - timedelta(minutes=8),
                ),
                ConfirmationScore(
                    agent_id=agent.agent_id,
                    validator_hotkey=_OTHER,
                    bench_version=_BENCH_VERSION,
                    seed=4_656_752_738_245_642_784,
                    composite=0.919667,
                    run_id="351dd771",
                    created_at=_NOW - timedelta(minutes=2),
                ),
            ]
        )

    samples = await retest_sample_inference(
        session, agent_id=agent.agent_id, bench_version=_BENCH_VERSION
    )

    assert [sample.run_id for sample in samples] == ["351dd771", "fd227604"]
    clean, low = samples
    assert clean.grant_id == healthy.grant_id
    assert (clean.chat_requests, clean.failed_requests, clean.failure_codes) == (
        3,
        0,
        (),
    )
    assert low.grant_id == retest.grant_id
    assert low.seed == 4_656_752_738_245_642_784
    assert low.chat_requests == 10
    assert low.failed_requests == 6
    assert low.failure_codes == (("upstream_http_402", 5), ("provider_timeout", 1))


async def test_sample_without_a_grant_reports_no_counts(session: AsyncSession) -> None:
    agent = Agent(
        agent_id=uuid4(),
        miner_hotkey="5Miner",
        name="nogrant",
        sha256=uuid4().hex * 2,
        status=AgentStatus.SCORED,
        created_at=_NOW,
    )
    async with session.begin():
        session.add(agent)
        await session.flush()
        session.add(
            ConfirmationScore(
                agent_id=agent.agent_id,
                validator_hotkey=_VALIDATOR,
                bench_version=_BENCH_VERSION,
                seed=7,
                composite=0.5,
                run_id="r",
                created_at=_NOW,
            )
        )

    [sample] = await retest_sample_inference(
        session, agent_id=agent.agent_id, bench_version=_BENCH_VERSION
    )
    assert sample.grant_id is None
    assert (sample.chat_requests, sample.failed_requests, sample.failure_codes) == (
        0,
        0,
        (),
    )
    assert (
        await retest_sample_inference(session, agent_id=uuid4(), bench_version=14) == []
    )
