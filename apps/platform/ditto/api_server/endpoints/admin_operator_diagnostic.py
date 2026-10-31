"""Audited isolation and read-back for operator-owned diagnostic agents."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import func, select

from ditto.api_models.operator_diagnostic import (
    ISOLATE_CONFIRMATION,
    OperatorDiagnosticIsolate,
    OperatorDiagnosticView,
    OperatorDiagnosticWithdrawal,
)
from ditto.api_models.ticket_status import TicketPurpose
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.api_server.endpoints.validator import SessionDep
from ditto.api_server.operator_diagnostic import (
    diagnostic_target_refusal,
    isolation_refusal,
    operator_diagnostic_hotkeys,
)
from ditto.db.models import Agent, Score, ValidatorQueueWithdrawal, ValidatorTicket
from ditto.db.queries.audit import append_audit_entry
from ditto.db.queries.benchmark_rollout import active_bench_version, open_rollout
from ditto.db.queries.queue_removal import is_in_force, removal_in_force

router = APIRouter(prefix="/admin/operator-diagnostic", tags=["admin"])
AdminDep = Annotated[None, Depends(require_admin)]


async def _queue_versions(session: SessionDep) -> list[int]:
    """Every benchmark version a newly screened agent could be queued for."""
    versions = {await active_bench_version(session)}
    rollout = await open_rollout(session)
    if rollout is not None:
        versions.add(rollout.desired_version)
    return sorted(versions)


async def _view(session: SessionDep, agent: Agent) -> OperatorDiagnosticView:
    hotkeys = operator_diagnostic_hotkeys()
    withdrawals = (
        await session.scalars(
            select(ValidatorQueueWithdrawal)
            .where(ValidatorQueueWithdrawal.agent_id == agent.agent_id)
            .order_by(ValidatorQueueWithdrawal.created_at)
        )
    ).all()
    tickets = (
        await session.scalars(
            select(ValidatorTicket).where(ValidatorTicket.agent_id == agent.agent_id)
        )
    ).all()
    score_count = await session.scalar(
        select(func.count()).where(Score.agent_id == agent.agent_id)
    )
    versions = sorted(
        {w.bench_version for w in withdrawals} | set(await _queue_versions(session))
    )
    return OperatorDiagnosticView(
        agent_id=agent.agent_id,
        miner_hotkey=agent.miner_hotkey,
        operator_owned=agent.miner_hotkey in hotkeys,
        shadow=agent.shadow,
        status=str(agent.status),
        withdrawals=[
            OperatorDiagnosticWithdrawal(
                bench_version=w.bench_version,
                created_at=w.created_at,
                in_force=is_in_force(w),
            )
            for w in withdrawals
        ],
        ordinary_lease_count=sum(
            1 for t in tickets if t.purpose != TicketPurpose.BENCHMARK_CANARY
        ),
        diagnostic_lease_count=sum(
            1 for t in tickets if t.purpose == TicketPurpose.BENCHMARK_CANARY
        ),
        score_count=int(score_count or 0),
        isolation_refusal=await isolation_refusal(
            session, agent, operator_hotkeys=hotkeys
        ),
        diagnostic_refusal_by_version={
            version: await diagnostic_target_refusal(
                session, agent, bench_version=version, operator_hotkeys=hotkeys
            )
            for version in versions
        },
    )


@router.get("/{agent_id}", response_model=OperatorDiagnosticView)
async def get_operator_diagnostic(
    agent_id: UUID, _admin: AdminDep, session: SessionDep
) -> OperatorDiagnosticView:
    """Read one agent's operator-diagnostic eligibility (read-only)."""
    async with session.begin():
        agent = await session.get(Agent, agent_id)
        if agent is None:
            raise HTTPException(404, "agent not found")
        return await _view(session, agent)


@router.post("/{agent_id}/isolate", response_model=OperatorDiagnosticView)
async def isolate_operator_diagnostic(
    agent_id: UUID,
    payload: OperatorDiagnosticIsolate,
    _admin: AdminDep,
    session: SessionDep,
    x_admin_actor: Annotated[str | None, Header()] = None,
) -> OperatorDiagnosticView:
    """Mark an operator-owned agent shadow and withhold it from validator queues.

    Allowed only before screening completes and while the agent has never been
    leased or scored, so the withdrawal is in force before ordinary dispatch can
    reach it. Writes one withdrawal per queueable benchmark version plus one
    hash-chained audit entry. Every other agent is refused.
    """
    if payload.confirmation != ISOLATE_CONFIRMATION:
        raise HTTPException(422, "exact isolation confirmation required")
    actor = (x_admin_actor or payload.actor).strip()
    if not 1 <= len(actor) <= 120:
        raise HTTPException(422, "actor is required")
    async with session.begin():
        agent = await session.get(Agent, agent_id, with_for_update=True)
        if agent is None:
            raise HTTPException(404, "agent not found")
        if agent.sha256 != payload.expected_artifact_sha256:
            raise HTTPException(409, "artifact digest does not match")
        refusal = await isolation_refusal(
            session, agent, operator_hotkeys=operator_diagnostic_hotkeys()
        )
        if refusal is not None:
            raise HTTPException(409, refusal)
        now = datetime.now(UTC)
        versions = await _queue_versions(session)
        agent.shadow = True
        for version in versions:
            if await session.scalar(
                select(ValidatorQueueWithdrawal.withdrawal_id).where(
                    ValidatorQueueWithdrawal.agent_id == agent.agent_id,
                    ValidatorQueueWithdrawal.bench_version == version,
                    removal_in_force(),
                )
            ):
                continue
            session.add(
                ValidatorQueueWithdrawal(
                    withdrawal_id=uuid.uuid4(),
                    agent_id=agent.agent_id,
                    bench_version=version,
                    actor=actor,
                    reason=payload.reason,
                    expected_snapshot="operator-diagnostic-isolation",
                    score_count=0,
                    ticket_snapshot=[],
                    created_at=now,
                )
            )
        await append_audit_entry(
            session,
            agent_id=agent.agent_id,
            validator_hotkey=None,
            event="operator_diagnostic_isolated",
            recorded_at=now,
            payload={
                "actor": actor,
                "reason": payload.reason,
                "artifact_sha256": agent.sha256,
                "bench_versions": versions,
                "status_at_isolation": str(agent.status),
            },
        )
        await session.flush()
        return await _view(session, agent)
