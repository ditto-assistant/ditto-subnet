"""The terminal artifact failure close-out and its periodic catch-up sweep."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.screener import SCREENING_POLICY_VERSION
from ditto.api_models.ticket_status import TicketStatus
from ditto.db.models import Agent, ValidatorQueueWithdrawal, ValidatorTicket
from ditto.db.queries.terminal_failure import (
    TERMINAL_ARTIFACT_FAILURE_ACTOR,
    sweep_terminal_artifact_failures,
    terminal_close_out_candidates,
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
