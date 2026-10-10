"""Trusted operator path that creates and binds one hosted-v2 shadow assignment.

The operator names a subject (agent, registered release, catalog index, validator and
approved profile digests). The artifact digest, screened image digest, registration
digest, bench version, schedule commitment and selection are derived here from locked
Platform state, never accepted from the request. Preview returns the exact authority;
create re-derives it and requires the operator to confirm that digest.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.agent_status import SCOREABLE_AGENT_STATUSES
from ditto.api_models.coding_canonical import coding_canonical_sha256
from ditto.api_models.coding_hosted_assignment_admin import (
    AdminHostedAssignmentCreated,
    AdminHostedAssignmentCreateRequest,
    AdminHostedAssignmentPlan,
    AdminHostedAssignmentPreviewRequest,
    AdminHostedAssignmentSubject,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.coding_hosted_private import HostedTaskSelection
from ditto.db.models import Agent, CodingPrivateV2Release
from ditto.db.queries.benchmark_rollout import active_bench_version
from ditto.db.queries.coding_certifications import active_validator_coding_certification
from ditto.db.queries.coding_hosted_admission import (
    HostedAdmissionError,
    HostedAssignmentAuthority,
    _now,
    create_hosted_assignment,
)
from ditto.db.queries.coding_hosted_private import (
    HostedPrivateTaskError,
    bind_hosted_private_task,
)

router = APIRouter(prefix="/admin/coding-hosted-assignments", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]

CODING_CONTRACT_VERSION = 2
# The only supported capability certification lease, receipt and persistence path
# is typed to contract v1 (coding_certification.py, coding_certifications.py), so
# a hosted-v2 subject is gated on its current v1 capability certification.
CERTIFICATION_CONTRACT_VERSION = 1


def canary_schedule_sha256(
    *,
    registration_sha256: str,
    catalog_index: int,
    evaluation_id: UUID,
    attempt_id: UUID,
) -> str:
    """Single-arm schedule commitment: the operator chose exactly this one index."""

    return coding_canonical_sha256(
        {
            "schema": "dittobench-coding-hosted-canary-schedule-v1",
            "coding_contract_version": CODING_CONTRACT_VERSION,
            "shadow_only": True,
            "weight_eligible": False,
            "registration_sha256": registration_sha256,
            "catalog_index": catalog_index,
            "evaluation_id": str(evaluation_id),
            "attempt_id": str(attempt_id),
        },
        maximum_bytes=4096,
        label="hosted canary schedule",
    )


def _confirmation(evaluation_id: UUID, assignment_sha256: str) -> str:
    return f"CREATE SHADOW CODING HOSTED ASSIGNMENT {evaluation_id} {assignment_sha256}"


async def _plan(
    session: AsyncSession,
    subject: AdminHostedAssignmentSubject,
    *,
    evaluation_id: UUID,
    attempt_id: UUID,
    deadline_unix: int,
) -> tuple[AdminHostedAssignmentPlan, HostedAssignmentAuthority, HostedTaskSelection]:
    release = await session.get(CodingPrivateV2Release, subject.release_row_id)
    if release is None:
        raise HTTPException(status_code=404, detail="private v2 release not found")
    agent = await session.get(Agent, subject.agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="agent not found")
    if (
        agent.screened_image_sha256 is None
        or agent.status not in SCOREABLE_AGENT_STATUSES
    ):
        raise HTTPException(
            status_code=409, detail="agent has no scoreable screened image"
        )
    now = await _now(session)
    deadline = datetime.fromtimestamp(deadline_unix, UTC)
    if not now < deadline or deadline - now > timedelta(hours=1):
        raise HTTPException(status_code=409, detail="assignment deadline is invalid")
    bench_version = await active_bench_version(session)
    certification = await active_validator_coding_certification(
        session,
        agent=agent,
        validator_hotkey=subject.validator_hotkey,
        bench_version=bench_version,
        coding_contract_version=CERTIFICATION_CONTRACT_VERSION,
        active_through=deadline,
    )
    if certification is None:
        raise HTTPException(
            status_code=409,
            detail="agent lacks an active coding certification through the deadline",
        )
    schedule_sha256 = canary_schedule_sha256(
        registration_sha256=release.registration_sha256,
        catalog_index=subject.catalog_index,
        evaluation_id=evaluation_id,
        attempt_id=attempt_id,
    )
    selection = HostedTaskSelection(
        evaluation_id=evaluation_id,
        attempt_id=attempt_id,
        registration_sha256=release.registration_sha256,
        artifact_sha256=agent.sha256,
        schedule_sha256=schedule_sha256,
        catalog_index=subject.catalog_index,
        max_patch_bytes=subject.max_patch_bytes,
    )
    authority = HostedAssignmentAuthority(
        evaluation_id=evaluation_id,
        attempt_id=attempt_id,
        release_row_id=subject.release_row_id,
        registration_sha256=release.registration_sha256,
        agent_id=subject.agent_id,
        validator_hotkey=subject.validator_hotkey,
        artifact_sha256=agent.sha256,
        screened_image_sha256=agent.screened_image_sha256,
        selection_sha256=selection.digest(),
        policy_sha256=subject.policy_sha256,
        execution_profile_sha256=subject.execution_profile_sha256,
        grading_profile_sha256=subject.grading_profile_sha256,
        deadline_unix=deadline_unix,
    )
    try:
        projection = authority.projection()
        selection_projection = selection.projection()
    except (HostedAdmissionError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    digest = authority.digest()
    plan = AdminHostedAssignmentPlan(
        evaluation_id=evaluation_id,
        attempt_id=attempt_id,
        deadline_unix=deadline_unix,
        artifact_sha256=agent.sha256,
        screened_image_sha256=agent.screened_image_sha256,
        registration_sha256=release.registration_sha256,
        bench_version=bench_version,
        certification_row_id=certification.certification_row_id,
        schedule_sha256=schedule_sha256,
        selection_sha256=selection.digest(),
        selection=selection_projection,
        assignment_sha256=digest,
        authority=projection,
        confirmation=_confirmation(evaluation_id, digest),
    )
    return plan, authority, selection


@router.post("/preview", response_model=AdminHostedAssignmentPlan)
async def preview_hosted_assignment(
    payload: AdminHostedAssignmentPreviewRequest,
    response: Response,
    _admin: AdminDep,
    session: SessionDep,
) -> AdminHostedAssignmentPlan:
    """Derive the exact authority without writing anything."""

    response.headers["Cache-Control"] = "no-store"
    now = await _now(session)
    plan, _, _ = await _plan(
        session,
        payload,
        evaluation_id=uuid4(),
        attempt_id=uuid4(),
        deadline_unix=int(now.timestamp()) + payload.lease_seconds,
    )
    return plan


@router.post("", response_model=AdminHostedAssignmentCreated)
async def create_hosted_assignment_endpoint(
    payload: AdminHostedAssignmentCreateRequest,
    response: Response,
    _admin: AdminDep,
    session: SessionDep,
) -> AdminHostedAssignmentCreated:
    """Re-derive the previewed authority, then create and bind it atomically."""

    response.headers["Cache-Control"] = "no-store"
    async with session.begin():
        plan, authority, selection = await _plan(
            session,
            payload,
            evaluation_id=payload.evaluation_id,
            attempt_id=payload.attempt_id,
            deadline_unix=payload.deadline_unix,
        )
        if (
            payload.confirmed_assignment_sha256 != plan.assignment_sha256
            or payload.confirmation != plan.confirmation
        ):
            raise HTTPException(
                status_code=422,
                detail=f'confirmation must equal "{plan.confirmation}"',
            )
        try:
            await create_hosted_assignment(
                session,
                authority=authority,
                confirmed_assignment_sha256=plan.assignment_sha256,
                actor=payload.actor,
                reason=payload.reason,
            )
            grants = await bind_hosted_private_task(session, selection=selection)
        except (HostedAdmissionError, HostedPrivateTaskError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
    return AdminHostedAssignmentCreated(
        **plan.model_dump(),
        authoring_grant_id=grants.authoring_grant_id,
        grading_grant_id=grants.grading_grant_id,
    )
