"""Admin read model for shadow coding capability certifications."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.coding_certification import (
    AgentCodingCertificationStatus,
    CodingCertificationRecord,
    CodingCertificationStage,
    CodingCertificationStatus,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.db.models import Agent
from ditto.db.queries.coding_certifications import (
    coding_certification_stale_reason,
    list_agent_coding_certifications,
    summarize_agent_coding_certifications,
)

router = APIRouter(prefix="/admin", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]


@router.get(
    "/agents/{agent_id}/coding-certifications",
    response_model=AgentCodingCertificationStatus,
)
async def agent_coding_certifications(
    agent_id: UUID,
    response: Response,
    _admin: AdminDep,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> AgentCodingCertificationStatus:
    """Return newest-first receipt history and current exact-artifact state."""

    response.headers["Cache-Control"] = "no-store"
    agent = await session.scalar(select(Agent).where(Agent.agent_id == agent_id))
    if agent is None:
        raise HTTPException(status_code=404, detail="agent not found")
    rows, total = await list_agent_coding_certifications(
        session, agent_id=agent_id, limit=limit
    )
    now = datetime.now(UTC)
    summary = await summarize_agent_coding_certifications(
        session,
        agent=agent,
        now=now,
    )
    records: list[CodingCertificationRecord] = []
    for row in rows:
        stale_reason = coding_certification_stale_reason(row, agent, now=now)
        records.append(
            CodingCertificationRecord(
                certification_row_id=row.certification_row_id,
                validator_hotkey=row.validator_hotkey,
                bench_version=row.bench_version,
                lease_id=row.lease_id,
                ticket_deadline=row.ticket_deadline,
                coding_contract_version=row.coding_contract_version,
                certification_id=row.certification_id,
                status=CodingCertificationStatus(row.status),
                failure_stage=(
                    CodingCertificationStage(row.failure_stage)
                    if row.failure_stage is not None
                    else None
                ),
                failure_code=row.failure_code,
                certification_sha256=row.certification_sha256,
                canary_manifest_sha256=row.canary_manifest_sha256,
                screened_image_sha256=row.screened_image_sha256,
                transcript_object_key=row.transcript_object_key,
                frozen_submission_object_key=row.frozen_submission_object_key,
                issued_at=row.issued_at,
                expires_at=row.expires_at,
                created_at=row.created_at,
                active=stale_reason == "active",
                stale_reason=stale_reason,
            )
        )
    return AgentCodingCertificationStatus(
        agent_id=agent.agent_id,
        agent_name=agent.name,
        miner_hotkey=agent.miner_hotkey,
        artifact_sha256=agent.sha256,
        screened_image_sha256=agent.screened_image_sha256,
        coding_supported=summary.coding_supported,
        coding_certified=summary.coding_certified,
        active_certification_count=summary.active_certification_count,
        total=total,
        certifications=records,
    )
