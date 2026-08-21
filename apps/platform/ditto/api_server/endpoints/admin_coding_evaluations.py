"""Admin visibility for the separate shadow coding evaluation ledger."""

from __future__ import annotations

from datetime import UTC, datetime
from statistics import median_low
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.coding_evaluation import (
    AgentCodingShadowEvaluationStatus,
    CodingSelectionAssignmentRecord,
    CodingShadowResultRecord,
    CodingShadowRunRecord,
    CodingShadowTicketRecord,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.db.models import (
    Agent,
    CodingCapabilityCertification,
    CodingSelectionAssignmentRow,
    CodingShadowRun,
    CoreQualificationObservation,
)
from ditto.db.queries.coding_assignments import (
    list_agent_coding_selection_assignments,
)
from ditto.db.queries.coding_catalog import active_coding_catalog_release
from ditto.db.queries.coding_certifications import coding_certification_stale_reason
from ditto.db.queries.coding_evaluations import list_agent_coding_shadow_runs
from ditto.db.queries.core_qualification import (
    latest_core_qualification_observation,
    latest_core_qualification_policy,
)

router = APIRouter(prefix="/admin", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]
_CODING_SHADOW_QUORUM = 3


async def _core_qualification_stale_reason(
    session: AsyncSession,
    *,
    agent: Agent,
    bench_version: int,
    observation_id: UUID,
) -> Literal["current", "policy_changed", "qualification_stale"]:
    observation = await session.get(CoreQualificationObservation, observation_id)
    policy = await latest_core_qualification_policy(
        session,
        bench_version=bench_version,
    )
    if (
        observation is None
        or policy is None
        or observation.policy_revision != policy.revision
    ):
        return "policy_changed"
    if agent.screened_image_sha256 is None:
        return "qualification_stale"
    latest = await latest_core_qualification_observation(
        session,
        agent_id=agent.agent_id,
        artifact_sha256=agent.sha256,
        screened_image_sha256=agent.screened_image_sha256,
        bench_version=bench_version,
        policy_revision=policy.revision,
    )
    if latest is None or not latest.complete_wave or not latest.qualified:
        return "qualification_stale"
    return "current"


async def _assignment_stale_reason(
    session: AsyncSession,
    *,
    agent: Agent,
    assignment: CodingSelectionAssignmentRow,
    now: datetime,
) -> Literal[
    "current",
    "artifact_changed",
    "screened_image_changed",
    "catalog_retired",
    "policy_changed",
    "qualification_stale",
    "certification_stale",
]:
    if assignment.artifact_sha256 != agent.sha256:
        return "artifact_changed"
    if assignment.screened_image_sha256 != agent.screened_image_sha256:
        return "screened_image_changed"
    if (
        await active_coding_catalog_release(
            session,
            corpus_release_id=assignment.corpus_release_id,
        )
        is None
    ):
        return "catalog_retired"
    qualification_reason = await _core_qualification_stale_reason(
        session,
        agent=agent,
        bench_version=assignment.bench_version,
        observation_id=assignment.core_qualification_observation_id,
    )
    if qualification_reason != "current":
        return qualification_reason
    certification = await session.get(
        CodingCapabilityCertification,
        assignment.certification_row_id,
    )
    if (
        certification is None
        or coding_certification_stale_reason(certification, agent, now=now) != "active"
    ):
        return "certification_stale"
    return "current"


async def _run_stale_reason(
    session: AsyncSession,
    *,
    agent: Agent,
    run: CodingShadowRun,
) -> Literal[
    "current",
    "artifact_changed",
    "screened_image_changed",
    "policy_changed",
    "qualification_stale",
    "catalog_retired",
]:
    if run.artifact_sha256 != agent.sha256:
        return "artifact_changed"
    if run.screened_image_sha256 != agent.screened_image_sha256:
        return "screened_image_changed"
    if (
        await active_coding_catalog_release(
            session,
            corpus_release_id=run.corpus_release_id,
        )
        is None
    ):
        return "catalog_retired"
    return await _core_qualification_stale_reason(
        session,
        agent=agent,
        bench_version=run.bench_version,
        observation_id=run.core_qualification_observation_id,
    )


@router.get(
    "/agents/{agent_id}/coding-shadow-evaluations",
    response_model=AgentCodingShadowEvaluationStatus,
)
async def agent_coding_shadow_evaluations(
    agent_id: UUID,
    response: Response,
    _admin: AdminDep,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
) -> AgentCodingShadowEvaluationStatus:
    """Return bounded run/ticket/result summaries without private task data."""

    response.headers["Cache-Control"] = "no-store"
    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="agent not found")
    now = datetime.now(UTC)
    assignment_rows, assignment_total = await list_agent_coding_selection_assignments(
        session,
        agent_id=agent_id,
        limit=limit,
    )
    assignments: list[CodingSelectionAssignmentRecord] = []
    for assignment in assignment_rows:
        stale_reason = await _assignment_stale_reason(
            session,
            agent=agent,
            assignment=assignment,
            now=now,
        )
        assignments.append(
            CodingSelectionAssignmentRecord(
                assignment_row_id=assignment.assignment_row_id,
                assignment_sha256=assignment.assignment_sha256,
                coding_run_id=assignment.coding_run_id,
                bench_version=assignment.bench_version,
                coding_contract_version=1,
                artifact_sha256=assignment.artifact_sha256,
                screened_image_sha256=assignment.screened_image_sha256,
                corpus_release_id=assignment.corpus_release_id,
                catalog_commitment_sha256=(assignment.catalog_commitment_sha256),
                anchor_block_number=assignment.anchor_block_number,
                anchor_block_hash=assignment.anchor_block_hash,
                selection_delay_blocks=assignment.selection_delay_blocks,
                selection_block_number=assignment.selection_block_number,
                assigned_at=assignment.assigned_at,
                task_count=1,
                core_qualification_observation_id=(
                    assignment.core_qualification_observation_id
                ),
                certification_row_id=assignment.certification_row_id,
                current=stale_reason == "current",
                stale_reason=stale_reason,
                created_at=assignment.created_at,
                weight_eligible=False,
            )
        )
    bundles, total = await list_agent_coding_shadow_runs(
        session,
        agent_id=agent_id,
        limit=limit,
    )
    records: list[CodingShadowRunRecord] = []
    for bundle in bundles:
        result_records: dict[UUID, CodingShadowResultRecord] = {}
        for ticket_id, result in bundle.results.items():
            ticket = next(
                item for item in bundle.tickets if item.ticket_id == ticket_id
            )
            result_records[ticket_id] = CodingShadowResultRecord(
                result_id=result.result_id,
                ticket_id=ticket_id,
                validator_hotkey=ticket.validator_hotkey,
                run_evidence_sha256=result.run_evidence_sha256,
                task_count=result.task_count,
                resolved_count=result.resolved_count,
                repair_failure_count=result.repair_failure_count,
                infrastructure_count=result.infrastructure_count,
                invalid_count=result.invalid_count,
                candidate_integrity_count=result.candidate_integrity_count,
                control_plane_integrity_count=result.control_plane_integrity_count,
                scoreable_task_count=result.scoreable_task_count,
                repair_mean_micros=result.repair_mean_micros,
                submitted_at=result.created_at,
                weight_eligible=False,
            )
        tickets = [
            CodingShadowTicketRecord(
                ticket_id=ticket.ticket_id,
                validator_hotkey=ticket.validator_hotkey,
                certification_row_id=ticket.certification_row_id,
                issued_at=ticket.issued_at,
                deadline=ticket.deadline,
                result=result_records.get(ticket.ticket_id),
            )
            for ticket in bundle.tickets
        ]
        stale_reason = await _run_stale_reason(
            session,
            agent=agent,
            run=bundle.run,
        )
        repair_means = sorted(
            result.repair_mean_micros for result in bundle.results.values()
        )
        quorum_complete = len(repair_means) >= _CODING_SHADOW_QUORUM
        records.append(
            CodingShadowRunRecord(
                run_row_id=bundle.run.run_row_id,
                coding_run_id=bundle.run.coding_run_id,
                bench_version=bundle.run.bench_version,
                coding_contract_version=1,
                artifact_sha256=bundle.run.artifact_sha256,
                screened_image_sha256=bundle.run.screened_image_sha256,
                corpus_release_id=bundle.run.corpus_release_id,
                run_manifest_sha256=bundle.run.run_manifest_sha256,
                task_set_manifest_sha256=bundle.run.task_set_manifest_sha256,
                task_count=bundle.run.task_count,
                core_qualification_observation_id=(
                    bundle.run.core_qualification_observation_id
                ),
                ticket_count=len(bundle.tickets),
                result_count=len(repair_means),
                quorum_complete=quorum_complete,
                median_repair_mean_micros=(
                    int(median_low(repair_means)) if quorum_complete else None
                ),
                current=stale_reason == "current",
                stale_reason=stale_reason,
                tickets=tickets,
                created_at=bundle.run.created_at,
                weight_eligible=False,
            )
        )
    return AgentCodingShadowEvaluationStatus(
        agent_id=agent.agent_id,
        agent_name=agent.name,
        miner_hotkey=agent.miner_hotkey,
        artifact_sha256=agent.sha256,
        screened_image_sha256=agent.screened_image_sha256,
        total_assignments=assignment_total,
        assignments=assignments,
        total_runs=total,
        runs=records,
        shadow_only=True,
    )
