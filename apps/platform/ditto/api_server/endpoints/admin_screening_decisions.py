"""Read the policy-v13 screening decision records Backroom cites.

``GET /admin/screening-decisions`` pages every recorded decision (filter by
outcome; ``review_timed_out`` counts are the activation monitor), and
``GET /admin/screening-decisions/{agent_id}`` returns one agent's history
newest first. Both fold in the published retry/deadline policy and capacity
thresholds so a timeout is always read next to the bar it was held to. Read
only: decisions are written by ``resolve_ath_review`` and the finalizer.
"""

from __future__ import annotations

from typing import Annotated, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.screening_decision import (
    AdminScreeningDecisionList,
    AdminScreeningDecisionRecordResponse,
    ScreeningDecisionIdentities,
    ScreeningDecisionOutcome,
    ScreeningDecisionRecordView,
    ScreeningFailureDomain,
    review_capacity_thresholds_view,
    review_timeout_policy_view,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.db.models import Agent, ScreeningDecisionRecord
from ditto_screening_protocol import REVIEW_TIMED_OUT_OUTCOME

router = APIRouter(prefix="/admin/screening-decisions", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]


def _strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _view(row: ScreeningDecisionRecord) -> ScreeningDecisionRecordView:
    identities = row.identities if isinstance(row.identities, dict) else {}
    return ScreeningDecisionRecordView(
        decision_id=row.decision_id,
        agent_id=row.agent_id,
        attempt_id=row.attempt_id,
        quarantine_id=row.quarantine_id,
        review_id=row.review_id,
        outcome=cast(ScreeningDecisionOutcome, row.outcome),
        reason_codes=_strings(row.reason_codes),
        violation_proven=row.violation_proven,
        failure_domain=cast(ScreeningFailureDomain, row.failure_domain),
        retry_count=row.retry_count,
        independent_workers=row.independent_workers,
        policy_version=row.policy_version,
        identities=ScreeningDecisionIdentities.model_validate(
            {
                "submission_uuid": identities.get("submission_uuid", row.agent_id),
                "artifact_sha256": identities.get("artifact_sha256", ""),
                "image_digest": identities.get("image_digest"),
                "build_configuration": identities.get("build_configuration"),
                "served_entrypoint": identities.get("served_entrypoint"),
                "permitted_runtime_configuration": identities.get(
                    "permitted_runtime_configuration"
                ),
                "benchmark_version": identities.get("benchmark_version"),
                "applied_policy_version": identities.get(
                    "applied_policy_version", row.policy_version
                ),
                "policy_digest": identities.get("policy_digest"),
                "verification_profile_digest": identities.get(
                    "verification_profile_digest"
                ),
            }
        ),
        review_scope=row.review_scope,
        completed_checks=_strings(row.completed_checks),
        failed_checks=_strings(row.failed_checks),
        opaque_components=_strings(row.opaque_components),
        evidence_references=_strings(row.evidence_references),
        evidence_type=row.evidence_type,
        limitations=_strings(row.limitations),
        public_reason=row.public_reason,
        reviewer=row.reviewer,
        decided_at=row.decided_at,
        supersedes_decision=row.supersedes_decision,
        operator_override=(
            row.operator_override if isinstance(row.operator_override, dict) else None
        ),
        precedent_weight=row.precedent_weight,
        retry_grant_id=row.retry_grant_id,
        is_verification_failure=(
            row.outcome == REVIEW_TIMED_OUT_OUTCOME
            or (row.outcome == "reject" and not row.violation_proven)
        ),
        no_fault=row.outcome == REVIEW_TIMED_OUT_OUTCOME,
    )


@router.get("", response_model=AdminScreeningDecisionList)
async def list_screening_decisions(
    _admin: AdminDep,
    session: SessionDep,
    outcome: Annotated[
        Literal["clear", "reject", "review_timed_out"] | None, Query()
    ] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AdminScreeningDecisionList:
    """Page decision records newest first, with subnet-wide outcome counts."""
    stmt = select(ScreeningDecisionRecord).order_by(
        ScreeningDecisionRecord.decided_at.desc(),
        ScreeningDecisionRecord.decision_id.desc(),
    )
    count_stmt = select(func.count()).select_from(ScreeningDecisionRecord)
    if outcome is not None:
        stmt = stmt.where(ScreeningDecisionRecord.outcome == outcome)
        count_stmt = count_stmt.where(ScreeningDecisionRecord.outcome == outcome)
    rows = list(await session.scalars(stmt.offset(offset).limit(limit)))
    count = await session.scalar(count_stmt)
    outcome_rows = (
        await session.execute(
            select(ScreeningDecisionRecord.outcome, func.count()).group_by(
                ScreeningDecisionRecord.outcome
            )
        )
    ).all()
    outcome_counts = dict.fromkeys(("clear", "reject", REVIEW_TIMED_OUT_OUTCOME), 0)
    for key, value in outcome_rows:
        outcome_counts[str(key)] = int(value or 0)
    return AdminScreeningDecisionList(
        items=[_view(row) for row in rows],
        count=int(count or 0),
        limit=limit,
        offset=offset,
        outcome=outcome,
        outcome_counts=outcome_counts,
        review_timeout_policy=review_timeout_policy_view(),
        review_capacity_thresholds=review_capacity_thresholds_view(),
    )


@router.get("/{agent_id}", response_model=AdminScreeningDecisionRecordResponse)
async def get_screening_decision_record(
    agent_id: UUID, _admin: AdminDep, session: SessionDep
) -> AdminScreeningDecisionRecordResponse:
    """Every decision recorded for one agent, newest first.

    An agent with no record is a 200 with an empty history, not a 404: the
    operator's question is "what was decided", and "nothing yet" answers it.
    """
    agent = await session.get(Agent, agent_id)
    rows = list(
        await session.scalars(
            select(ScreeningDecisionRecord)
            .where(ScreeningDecisionRecord.agent_id == agent_id)
            .order_by(
                ScreeningDecisionRecord.decided_at.desc(),
                ScreeningDecisionRecord.decision_id.desc(),
            )
        )
    )
    decisions = [_view(row) for row in rows]
    return AdminScreeningDecisionRecordResponse(
        agent_id=agent_id,
        agent_status=agent.status.value if agent is not None else None,
        latest=decisions[0] if decisions else None,
        decisions=decisions,
        review_timeout_policy=review_timeout_policy_view(),
        review_capacity_thresholds=review_capacity_thresholds_view(),
    )
