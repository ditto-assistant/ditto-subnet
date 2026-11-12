"""The terminal artifact failure close-out and its periodic catch-up sweep."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.screener import SCREENING_POLICY_VERSION
from ditto.api_models.ticket_status import TicketPurpose, TicketStatus
from ditto.db.models import Agent, ValidatorQueueWithdrawal, ValidatorTicket
from ditto.db.queries.scores import upsert_score
from ditto.db.queries.terminal_failure import (
    TERMINAL_ARTIFACT_FAILURE_ACTOR,
    sweep_terminal_artifact_failures,
    terminal_close_out_candidates,
    withdraw_terminal_artifact_failure,
)

_VERSION = 7
_NOW = datetime.now(UTC)


async def _seed(
    maker: async_sessionmaker[AsyncSession],
    *,
    name: str,
    details: list[str | None],
    live: bool = False,
) -> UUID:
    agent_id = uuid4()
    async with maker() as session, session.begin():
        session.add(
            Agent(
                agent_id=agent_id,
                miner_hotkey=f"5Miner-{name}",
                name=name,
                version=1,
                sha256=f"{abs(hash(name)) % (16**64):064x}",
                status=AgentStatus.EVALUATING,
                screening_policy_version=SCREENING_POLICY_VERSION,
                created_at=_NOW - timedelta(days=1),
            )
        )
        await session.flush()
        for index, detail in enumerate(details):
            failed = _NOW - timedelta(hours=1, minutes=index)
            session.add(
                ValidatorTicket(
                    agent_id=agent_id,
                    validator_hotkey=f"5Validator-{index}",
                    status=TicketStatus.EXPIRED,
                    issued_at=failed - timedelta(minutes=30),
                    deadline=failed,
                    bench_version=_VERSION,
                    attempt_count=1,
                    manual_retry_grants=0,
                    infra_retry_grants=0,
                    failure_reason="scoring_error" if detail else None,
                    failure_detail=detail,
                    failed_at=failed if detail else None,
                    retry_after=failed,
                )
            )
        if live:
            session.add(
                ValidatorTicket(
                    agent_id=agent_id,
                    validator_hotkey="5Validator-live",
                    status=TicketStatus.ISSUED,
                    issued_at=_NOW - timedelta(minutes=5),
                    deadline=_NOW + timedelta(hours=1),
                    bench_version=_VERSION,
                    attempt_count=1,
                    manual_retry_grants=0,
                    infra_retry_grants=0,
                )
            )
    return agent_id


async def _withdrawals(
    maker: async_sessionmaker[AsyncSession], agent_id: UUID
) -> list[ValidatorQueueWithdrawal]:
    async with maker() as session:
        return list(
            (
                await session.scalars(
                    select(ValidatorQueueWithdrawal).where(
                        ValidatorQueueWithdrawal.agent_id == agent_id
                    )
                )
            ).all()
        )


async def test_sweep_closes_out_a_row_the_failure_hook_missed(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """A stranded row (kgmin1105) is closed out once, and only once."""
    stranded = await _seed(
        session_maker, name="stranded", details=["inference_allowance_exhausted"] * 4
    )
    refused = await _seed(
        session_maker,
        name="refused",
        details=["inference_request_rejected:request_too_large"] * 3,
    )

    assert await sweep_terminal_artifact_failures(session_maker) == 2
    [withdrawal] = await _withdrawals(session_maker, stranded)
    assert withdrawal.actor == TERMINAL_ARTIFACT_FAILURE_ACTOR
    assert withdrawal.terminal_failure_code == "inference_allowance_exhausted"
    assert withdrawal.bench_version == _VERSION
    [refusal] = await _withdrawals(session_maker, refused)
    assert (
        refusal.terminal_failure_code == "inference_request_rejected:request_too_large"
    )

    # Idempotent: a withdrawn row is no longer a candidate.
    async with session_maker() as session:
        assert stranded not in await terminal_close_out_candidates(session, limit=100)
    assert await sweep_terminal_artifact_failures(session_maker) == 0
    assert len(await _withdrawals(session_maker, stranded)) == 1


async def test_sweep_leaves_rows_that_can_still_finish_or_are_not_the_agents(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    reachable = await _seed(
        session_maker, name="reachable", details=["inference_allowance_exhausted"] * 2
    )
    mixed = await _seed(
        session_maker,
        name="mixed",
        details=[
            "inference_allowance_exhausted",
            "inference_allowance_exhausted",
            "model_relay_unavailable",
        ],
    )
    disagreeing = await _seed(
        session_maker,
        name="disagreeing",
        details=[
            "inference_allowance_exhausted",
            "inference_allowance_exhausted",
            "model_inference_required",
        ],
    )
    running = await _seed(
        session_maker,
        name="running",
        details=["inference_allowance_exhausted"] * 3,
        live=True,
    )

    assert await sweep_terminal_artifact_failures(session_maker) == 0
    for agent_id in (reachable, mixed, disagreeing, running):
        assert await _withdrawals(session_maker, agent_id) == []


async def test_candidates_skip_rows_that_can_never_qualify(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Non-qualifying rows must not fill the batch ahead of qualifying ones."""
    unclassified = await _seed(
        session_maker,
        name="unclassified",
        details=["inference_request_rejected:stale_session"] * 3,
    )
    reachable = await _seed(
        session_maker, name="reachable", details=["inference_allowance_exhausted"] * 2
    )
    qualifying = await _seed(
        session_maker, name="qualifying", details=["inference_allowance_exhausted"] * 3
    )
    async with session_maker() as session:
        assert await terminal_close_out_candidates(session, limit=1) == [qualifying]
        candidates = await terminal_close_out_candidates(session, limit=100)
    assert unclassified not in candidates
    assert reachable not in candidates


async def test_a_prior_era_withdrawal_does_not_hide_the_current_era(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    agent_id = await _seed(
        session_maker, name="two-eras", details=["inference_allowance_exhausted"] * 3
    )
    async with session_maker() as session, session.begin():
        session.add(
            ValidatorQueueWithdrawal(
                withdrawal_id=uuid4(),
                agent_id=agent_id,
                bench_version=_VERSION - 1,
                actor="peyton",
                reason="earlier era withdrawal",
                expected_snapshot="ab" * 32,
                score_count=0,
                ticket_snapshot=[],
                created_at=_NOW - timedelta(days=2),
            )
        )
    assert await sweep_terminal_artifact_failures(session_maker) == 1
    current = [
        row
        for row in await _withdrawals(session_maker, agent_id)
        if row.bench_version == _VERSION
    ]
    assert [row.terminal_failure_code for row in current] == [
        "inference_allowance_exhausted"
    ]


async def test_an_agent_failing_in_two_versions_takes_one_candidate_slot(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    older = await _seed(
        session_maker, name="older", details=["inference_allowance_exhausted"] * 3
    )
    async with session_maker() as session, session.begin():
        agent = await session.get(Agent, older)
        assert agent is not None
        agent.created_at = _NOW - timedelta(days=2)
        for index in range(3):
            failed = _NOW - timedelta(hours=2, minutes=index)
            session.add(
                ValidatorTicket(
                    agent_id=older,
                    validator_hotkey=f"5Validator-{index}",
                    status=TicketStatus.EXPIRED,
                    issued_at=failed - timedelta(minutes=30),
                    deadline=failed,
                    bench_version=_VERSION + 1,
                    attempt_count=1,
                    manual_retry_grants=0,
                    infra_retry_grants=0,
                    failure_reason="scoring_error",
                    failure_detail="inference_allowance_exhausted",
                    failed_at=failed,
                    retry_after=failed,
                )
            )
    newer = await _seed(
        session_maker, name="newer", details=["inference_allowance_exhausted"] * 3
    )
    async with session_maker() as session:
        assert await terminal_close_out_candidates(session, limit=2) == [older, newer]


async def test_reinstatement_is_an_override_for_automatic_close_out(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    agent_id = await _seed(
        session_maker, name="reinstated", details=["inference_allowance_exhausted"] * 3
    )
    assert await sweep_terminal_artifact_failures(session_maker) == 1
    async with session_maker() as session, session.begin():
        withdrawal = await session.scalar(
            select(ValidatorQueueWithdrawal).where(
                ValidatorQueueWithdrawal.agent_id == agent_id
            )
        )
        assert withdrawal is not None
        withdrawal.reinstated_at = _NOW
    async with session_maker() as session, session.begin():
        assert await terminal_close_out_candidates(session, limit=1) == []
        assert (
            await withdraw_terminal_artifact_failure(
                session, agent_id=agent_id, now=_NOW
            )
            is None
        )
    assert await sweep_terminal_artifact_failures(session_maker) == 0
    assert len(await _withdrawals(session_maker, agent_id)) == 1


async def test_retryable_and_old_era_evidence_do_not_starve_current_candidates(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    retryable = await _seed(
        session_maker, name="retryable", details=["inference_allowance_exhausted"] * 3
    )
    old_era = await _seed(
        session_maker, name="old-era", details=["inference_allowance_exhausted"] * 3
    )
    async with session_maker() as session, session.begin():
        for agent_id in (retryable, old_era):
            agent = await session.get(Agent, agent_id)
            assert agent is not None
            agent.created_at = _NOW - timedelta(days=2)
        tickets = list(
            (
                await session.scalars(
                    select(ValidatorTicket).where(ValidatorTicket.agent_id == retryable)
                )
            ).all()
        )
        for ticket in tickets:
            ticket.manual_retry_grants = 1
        session.add(
            ValidatorTicket(
                agent_id=old_era,
                validator_hotkey="new-era-validator",
                status=TicketStatus.EXPIRED,
                issued_at=_NOW - timedelta(minutes=10),
                deadline=_NOW - timedelta(minutes=5),
                bench_version=_VERSION + 1,
                attempt_count=1,
                manual_retry_grants=0,
                infra_retry_grants=0,
                failure_reason="scoring_error",
                failure_detail="inference_allowance_exhausted",
                failed_at=_NOW - timedelta(minutes=5),
            )
        )
    qualifying = await _seed(
        session_maker, name="current-era", details=["inference_allowance_exhausted"] * 3
    )
    async with session_maker() as session:
        assert await terminal_close_out_candidates(session, limit=1) == [qualifying]
    assert await sweep_terminal_artifact_failures(session_maker, limit=1) == 1
    assert await sweep_terminal_artifact_failures(session_maker, limit=1) == 0


@pytest.mark.parametrize("with_score", [False, True])
async def test_generator_history_uses_score_or_canonical_era_fallback(
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    with_score: bool,
) -> None:
    agent_id = await _seed(
        session_maker,
        name="round-history",
        details=["inference_allowance_exhausted"] * 3,
    )
    # A score must win over a different canonical era; otherwise canonical
    # is the fallback used by both candidate selection and the locked decider.
    monkeypatch.setattr(
        "ditto.db.queries.terminal_failure.active_bench_version",
        AsyncMock(return_value=_VERSION + 1 if with_score else _VERSION),
    )
    async with session_maker() as session, session.begin():
        tickets = list(
            (
                await session.scalars(
                    select(ValidatorTicket).where(ValidatorTicket.agent_id == agent_id)
                )
            ).all()
        )
        for ticket in tickets:
            ticket.purpose = TicketPurpose.GENERATOR_ROUND
        if with_score:
            await upsert_score(
                session,
                agent_id=agent_id,
                validator_hotkey="scored-validator",
                run_id="scored-run",
                seed=42,
                composite=0.5,
                tool_mean=0.5,
                memory_mean=0.5,
                median_ms=500,
                n=20,
                generated_at=_NOW,
                bench_version=_VERSION,
            )
    async with session_maker() as session:
        assert await terminal_close_out_candidates(session, limit=1) == [agent_id]
    assert await sweep_terminal_artifact_failures(session_maker) == 1
    [withdrawal] = await _withdrawals(session_maker, agent_id)
    assert withdrawal.bench_version == _VERSION
