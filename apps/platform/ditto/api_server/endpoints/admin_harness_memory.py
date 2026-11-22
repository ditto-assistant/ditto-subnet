"""Backroom reads of per-agent harness memory (SN-500 step 1).

Operator-only: peak and time-weighted P50/P95 of the harness cgroup, per run
and rolled up per agent version. Never published to miners (whether miners
see their own numbers is an open decision), and nothing here feeds scoring.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.harness_memory import (
    AgentHarnessMemory,
    HarnessMemoryList,
    HarnessMemoryRollupView,
    HarnessMemoryRunView,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.db.models import Agent
from ditto.db.queries.harness_memory import (
    HarnessMemoryRollup,
    agent_harness_memory,
    list_harness_memory,
)

router = APIRouter(tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]

_COVERAGE_NOTE = (
    "Observations start with the release that added harness memory telemetry "
    "(SN-500); runs before it have no memory data, so absent agents were not "
    "measured, not light."
)


async def _agent_labels(
    session: AsyncSession, agent_ids: list[UUID]
) -> dict[UUID, tuple[str, str]]:
    if not agent_ids:
        return {}
    rows = await session.execute(
        select(Agent.agent_id, Agent.name, Agent.miner_hotkey).where(
            Agent.agent_id.in_(agent_ids)
        )
    )
    return {row.agent_id: (row.name, row.miner_hotkey) for row in rows}


def _view(
    rollup: HarnessMemoryRollup, labels: dict[UUID, tuple[str, str]]
) -> HarnessMemoryRollupView:
    name, hotkey = labels.get(rollup.agent_id, (None, None))
    return HarnessMemoryRollupView(
        agent_id=rollup.agent_id,
        agent_name=name,
        miner_hotkey=hotkey,
        runs=rollup.runs,
        scored_runs=rollup.scored_runs,
        failed_runs=rollup.failed_runs,
        oom_runs=rollup.oom_runs,
        peak_bytes_max=rollup.peak_bytes_max,
        p50_bytes_median=rollup.p50_bytes_median,
        p95_bytes_median=rollup.p95_bytes_median,
        p95_bytes_max=rollup.p95_bytes_max,
        limit_bytes_max=rollup.limit_bytes_max,
        last_recorded_at=rollup.last_recorded_at,
    )


@router.get("/admin/harness-memory", response_model=HarnessMemoryList)
async def get_harness_memory(
    _admin: AdminDep,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> HarnessMemoryList:
    """Agents with memory observations, heaviest P95 first."""
    # One extra row tells the caller whether another page exists.
    rollups = await list_harness_memory(session, limit=limit + 1, offset=offset)
    page = rollups[:limit]
    labels = await _agent_labels(session, [rollup.agent_id for rollup in page])
    return HarnessMemoryList(
        generated_at=datetime.now(UTC),
        agents=[_view(rollup, labels) for rollup in page],
        offset=offset,
        has_more=len(rollups) > limit,
        note=_COVERAGE_NOTE,
    )


@router.get(
    "/admin/agents/{agent_id}/harness-memory", response_model=AgentHarnessMemory
)
async def get_agent_harness_memory(
    agent_id: UUID,
    _admin: AdminDep,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> AgentHarnessMemory:
    """One agent version's rollup and newest observed runs."""
    rollup, runs = await agent_harness_memory(session, agent_id=agent_id, limit=limit)
    labels = await _agent_labels(session, [agent_id] if rollup is not None else [])
    return AgentHarnessMemory(
        agent_id=agent_id,
        rollup=_view(rollup, labels) if rollup is not None else None,
        runs=[
            HarnessMemoryRunView(
                validator_hotkey=run.validator_hotkey,
                bench_version=run.bench_version,
                lease_issued_at=run.lease_issued_at,
                outcome="scored" if run.outcome == "scored" else "failed",
                failure_reason=run.failure_reason,
                failure_detail=run.failure_detail,
                peak_bytes=run.peak_bytes,
                p50_bytes=run.p50_bytes,
                p95_bytes=run.p95_bytes,
                samples=run.samples,
                sampled_seconds=run.sampled_seconds,
                limit_bytes=run.limit_bytes,
                recorded_at=run.recorded_at,
            )
            for run in runs
        ],
    )
