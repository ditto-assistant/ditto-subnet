"""Append-only, digest-only V13 pre-randomness generation registry.

Recording a group is an audit event, not permission to issue seeds or clear a
hold. A future protected provisioner must fetch and verify this committed
event before invoking CSPRNG or exposing any private case bytes.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.v13_private_generation import (
    V13GenerationGroupView,
    V13GenerationStartRequest,
    V13KnownBenignApprovalRequest,
    V13KnownBenignApprovalView,
    V13KnownBenignAttestationRequest,
    V13KnownBenignProvenanceView,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.api_server.v13_benign_identity import verify_v13_benign_assertion
from ditto.db.models import (
    Agent,
    ScreenedImageUpload,
    ScreeningAttempt,
    ScreeningPrivatePackageRegistration,
    V13KnownBenignAttestation,
    V13KnownBenignControlApproval,
    V13PrivateGenerationGroup,
)
from ditto_screening_protocol.v13_private_package import V13_PRIVATE_PROFILE_SHA256

router = APIRouter(prefix="/admin/v13-private-generation", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]


def _actor(value: str | None) -> str:
    if value is None or not 1 <= len(value.strip()) <= 120:
        raise HTTPException(status_code=422, detail="X-Admin-Actor is required")
    return value.strip()


async def _database_now(session: AsyncSession) -> datetime:
    clock = (
        func.clock_timestamp()
        if session.get_bind().dialect.name == "postgresql"
        else func.current_timestamp()
    )
    value = await session.scalar(select(clock))
    if value is None:
        raise HTTPException(status_code=503, detail="database clock unavailable")
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


def _canonical_digest(value: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _utc_stamp(value: datetime) -> str:
    return (
        value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )


def generation_role_digest(
    group: V13PrivateGenerationGroup, role: Literal["target", "known_benign"]
) -> str:
    """Canonical #2177-compatible digest, including both exact role identities."""
    return _canonical_digest(
        {
            "revision": "v13-generation-group-v1",
            "group_id": str(group.group_id),
            "role": role,
            "target_agent_id": str(group.target_agent_id),
            "target_attempt_id": str(group.target_attempt_id),
            "target_artifact_sha256": group.target_artifact_sha256,
            "target_image_sha256": group.target_image_sha256,
            "control_agent_id": str(group.control_agent_id),
            "control_attempt_id": str(group.control_attempt_id),
            "control_artifact_sha256": group.control_artifact_sha256,
            "control_image_sha256": group.control_image_sha256,
            "approval_id": str(group.approval_id),
            "profile_sha256": group.profile_sha256,
            "started_at": _utc_stamp(group.started_at),
        }
    )


async def _bound_image(
    session: AsyncSession, *, agent: Agent, attempt: ScreeningAttempt, image: str
) -> bool:
    return bool(
        await session.scalar(
            select(ScreenedImageUpload.image_upload_id).where(
                ScreenedImageUpload.agent_id == agent.agent_id,
                ScreenedImageUpload.attempt_id == attempt.attempt_id,
                ScreenedImageUpload.screener_hotkey == attempt.screener_hotkey,
                ScreenedImageUpload.sha256 == image,
                ScreenedImageUpload.status == "verified",
            )
        )
    )


def _approval_view(row: V13KnownBenignControlApproval) -> V13KnownBenignApprovalView:
    return V13KnownBenignApprovalView.model_validate(
        {
            key: getattr(row, key)
            for key in V13KnownBenignApprovalView.model_fields
            if key != "status"
        }
    )


def _group_view(row: V13PrivateGenerationGroup) -> V13GenerationGroupView:
    return V13GenerationGroupView.model_validate(
        {
            key: getattr(row, key)
            for key in V13GenerationGroupView.model_fields
            if key != "status"
        }
    )


@router.post("/known-benign-approvals", response_model=V13KnownBenignApprovalView)
async def record_known_benign_approval(
    payload: V13KnownBenignApprovalRequest,
    _admin: AdminDep,
    session: SessionDep,
    x_admin_actor: Annotated[str | None, Header()] = None,
) -> V13KnownBenignApprovalView:
    """Audit an operator-claimed clean candidate; no semantic pass inferred.

    X-Admin-Actor is an audit label, not proof of an independent approver.
    """
    actor = _actor(x_admin_actor)
    if payload.profile_sha256 != V13_PRIVATE_PROFILE_SHA256:
        raise HTTPException(status_code=409, detail="V13 profile mismatch")
    try:
        async with session.begin():
            agent = await session.get(Agent, payload.agent_id, with_for_update=True)
            attempt = await session.get(
                ScreeningAttempt, payload.attempt_id, with_for_update=True
            )
            if (
                agent is None
                or attempt is None
                or attempt.agent_id != payload.agent_id
                or attempt.policy_version != 13
                or attempt.artifact_sha256 != payload.artifact_sha256
                or agent.sha256.lower() != payload.artifact_sha256
                or agent.status not in {AgentStatus.SCORED, AgentStatus.LIVE}
                or not await _bound_image(
                    session, agent=agent, attempt=attempt, image=payload.image_sha256
                )
            ):
                raise HTTPException(
                    status_code=409, detail="clean image guard mismatch"
                )
            existing = await session.scalar(
                select(V13KnownBenignControlApproval).where(
                    V13KnownBenignControlApproval.attempt_id == payload.attempt_id,
                    V13KnownBenignControlApproval.profile_sha256
                    == payload.profile_sha256,
                )
            )
            if existing is not None:
                if (
                    existing.artifact_sha256 != payload.artifact_sha256
                    or existing.image_sha256 != payload.image_sha256
                    or existing.review_evidence_sha256 != payload.review_evidence_sha256
                ):
                    raise HTTPException(status_code=409, detail="approval conflicts")
                return _approval_view(existing)
            approved_at = await _database_now(session)
            approval_id = uuid4()
            receipt_sha = _canonical_digest(
                {
                    "revision": "v13-known-benign-approval-v1",
                    "approval_id": str(approval_id),
                    "agent_id": str(payload.agent_id),
                    "attempt_id": str(payload.attempt_id),
                    "artifact_sha256": payload.artifact_sha256,
                    "image_sha256": payload.image_sha256,
                    "profile_sha256": payload.profile_sha256,
                    "review_evidence_sha256": payload.review_evidence_sha256,
                    "actor": actor,
                    "approved_at": _utc_stamp(approved_at),
                }
            )
            row = V13KnownBenignControlApproval(
                approval_id=approval_id,
                agent_id=payload.agent_id,
                attempt_id=payload.attempt_id,
                artifact_sha256=payload.artifact_sha256,
                image_sha256=payload.image_sha256,
                profile_sha256=payload.profile_sha256,
                review_evidence_sha256=payload.review_evidence_sha256,
                approval_receipt_sha256=receipt_sha,
                actor=actor,
                reason=payload.reason,
                approved_at=approved_at,
            )
            session.add(row)
            await session.flush()
            return _approval_view(row)
    except IntegrityError as error:
        raise HTTPException(
            status_code=409, detail="approval changed concurrently"
        ) from error


async def _provenance(
    session: AsyncSession, approval: V13KnownBenignControlApproval
) -> V13KnownBenignProvenanceView:
    reviewers = list(
        await session.scalars(
            select(V13KnownBenignAttestation)
            .where(
                V13KnownBenignAttestation.approval_id == approval.approval_id,
                V13KnownBenignAttestation.review_evidence_sha256
                == approval.review_evidence_sha256,
            )
            .order_by(V13KnownBenignAttestation.principal_sub)
        )
    )
    count = min(len(reviewers), 2)
    complete = reviewers[:2] if count == 2 else []
    return V13KnownBenignProvenanceView(
        approval_id=approval.approval_id,
        review_evidence_sha256=approval.review_evidence_sha256,
        authenticated_reviewers=count,
        status=(
            "two_person_authenticated"
            if count == 2
            else "one_authenticated_reviewer"
            if count == 1
            else "recorded_unverified"
        ),
        provenance_receipt_sha256=(
            _canonical_digest(
                {
                    "revision": "v13-known-benign-provenance-v1",
                    "approval_receipt_sha256": approval.approval_receipt_sha256,
                    "review_evidence_sha256": approval.review_evidence_sha256,
                    "reviewers": [
                        {
                            "principal_sub": row.principal_sub,
                            "assertion_sha256": row.assertion_sha256,
                            "attested_at": _utc_stamp(row.attested_at),
                        }
                        for row in complete
                    ],
                }
            )
            if complete
            else None
        ),
        completed_at=max((row.attested_at for row in complete), default=None),
    )


@router.get(
    "/known-benign-approvals/{approval_id}/provenance",
    response_model=V13KnownBenignProvenanceView,
)
async def get_known_benign_provenance(
    approval_id: UUID, _admin: AdminDep, session: SessionDep
) -> V13KnownBenignProvenanceView:
    approval = await session.get(V13KnownBenignControlApproval, approval_id)
    if approval is None:
        raise HTTPException(status_code=404, detail="approval not found")
    return await _provenance(session, approval)


@router.post(
    "/known-benign-approvals/{approval_id}/attest",
    response_model=V13KnownBenignProvenanceView,
)
async def attest_known_benign_approval(
    approval_id: UUID,
    payload: V13KnownBenignAttestationRequest,
    request: Request,
    _admin: AdminDep,
    session: SessionDep,
) -> V13KnownBenignProvenanceView:
    """Record one Google-session reviewer; shared bearer/actor cannot count."""
    try:
        async with session.begin():
            approval = await session.get(
                V13KnownBenignControlApproval, approval_id, with_for_update=True
            )
            if approval is None:
                raise HTTPException(status_code=404, detail="approval not found")
            principal = verify_v13_benign_assertion(
                payload.assertion,
                secret=request.app.state.config.v13_benign_attestation_secret,
                approval_id=approval_id,
                evidence_sha256=approval.review_evidence_sha256,
            )
            existing = await session.scalar(
                select(V13KnownBenignAttestation).where(
                    V13KnownBenignAttestation.approval_id == approval_id,
                    V13KnownBenignAttestation.principal_sub == principal.sub,
                )
            )
            if existing is not None:
                if existing.principal_email != principal.email:
                    raise HTTPException(
                        status_code=409, detail="reviewer identity changed"
                    )
                return await _provenance(session, approval)
            session.add(
                V13KnownBenignAttestation(
                    attestation_id=uuid4(),
                    approval_id=approval_id,
                    principal_sub=principal.sub,
                    principal_email=principal.email,
                    review_evidence_sha256=approval.review_evidence_sha256,
                    assertion_sha256=principal.assertion_sha256,
                    reason=payload.reason,
                    attested_at=await _database_now(session),
                )
            )
            await session.flush()
            return await _provenance(session, approval)
    except IntegrityError as error:
        raise HTTPException(
            status_code=409, detail="attestation changed concurrently"
        ) from error


@router.post("/groups", response_model=V13GenerationGroupView)
async def record_generation_start(
    payload: V13GenerationStartRequest,
    _admin: AdminDep,
    session: SessionDep,
    x_admin_actor: Annotated[str | None, Header()] = None,
) -> V13GenerationGroupView:
    """Commit one target-specific, two-role event before any seed issuance."""
    actor = _actor(x_admin_actor)
    if payload.profile_sha256 != V13_PRIVATE_PROFILE_SHA256:
        raise HTTPException(status_code=409, detail="V13 profile mismatch")
    try:
        async with session.begin():
            approval = await session.get(
                V13KnownBenignControlApproval, payload.approval_id
            )
            target = await session.get(
                Agent, payload.target_agent_id, with_for_update=True
            )
            attempt = await session.get(
                ScreeningAttempt, payload.target_attempt_id, with_for_update=True
            )
            if approval is None or target is None or attempt is None:
                raise HTTPException(
                    status_code=404, detail="generation identity not found"
                )
            control = await session.get(Agent, approval.agent_id, with_for_update=True)
            control_attempt = await session.get(
                ScreeningAttempt, approval.attempt_id, with_for_update=True
            )
            if (
                control is None
                or control_attempt is None
                or target.agent_id == control.agent_id
                or target.sha256.lower() != payload.target_artifact_sha256
                or attempt.agent_id != target.agent_id
                or attempt.policy_version != 13
                or attempt.artifact_sha256 != payload.target_artifact_sha256
                or target.status
                not in {AgentStatus.QUARANTINED, AgentStatus.ATH_PENDING_REVIEW}
                or control.status not in {AgentStatus.SCORED, AgentStatus.LIVE}
                or control.sha256.lower() != approval.artifact_sha256
                or control_attempt.agent_id != control.agent_id
                or control_attempt.policy_version != 13
                or control_attempt.artifact_sha256 != approval.artifact_sha256
                or approval.profile_sha256 != payload.profile_sha256
                or payload.target_artifact_sha256 == approval.artifact_sha256
                or payload.target_image_sha256 == approval.image_sha256
                or not await _bound_image(
                    session,
                    agent=target,
                    attempt=attempt,
                    image=payload.target_image_sha256,
                )
                or not await _bound_image(
                    session,
                    agent=control,
                    attempt=control_attempt,
                    image=approval.image_sha256,
                )
            ):
                raise HTTPException(status_code=409, detail="generation guard mismatch")
            if (
                await session.get(
                    ScreeningPrivatePackageRegistration, attempt.attempt_id
                )
                is not None
            ):
                raise HTTPException(
                    status_code=409, detail="package predates generation start"
                )
            existing = await session.scalar(
                select(V13PrivateGenerationGroup).where(
                    V13PrivateGenerationGroup.target_attempt_id == attempt.attempt_id
                )
            )
            if existing is not None:
                if (
                    existing.target_artifact_sha256 != payload.target_artifact_sha256
                    or existing.target_image_sha256 != payload.target_image_sha256
                    or existing.approval_id != payload.approval_id
                    or existing.profile_sha256 != payload.profile_sha256
                ):
                    raise HTTPException(status_code=409, detail="generation conflicts")
                return _group_view(existing)
            started_at = await _database_now(session)
            if started_at <= approval.approved_at:
                raise HTTPException(
                    status_code=409, detail="approval not prior to start"
                )
            row = V13PrivateGenerationGroup(
                group_id=uuid4(),
                target_agent_id=target.agent_id,
                target_attempt_id=attempt.attempt_id,
                target_artifact_sha256=payload.target_artifact_sha256,
                target_image_sha256=payload.target_image_sha256,
                control_agent_id=control.agent_id,
                control_attempt_id=control_attempt.attempt_id,
                control_artifact_sha256=approval.artifact_sha256,
                control_image_sha256=approval.image_sha256,
                approval_id=approval.approval_id,
                approval_receipt_sha256=approval.approval_receipt_sha256,
                profile_sha256=payload.profile_sha256,
                target_receipt_sha256="",
                control_receipt_sha256="",
                actor=actor,
                started_at=started_at,
            )
            row.target_receipt_sha256 = generation_role_digest(row, "target")
            row.control_receipt_sha256 = generation_role_digest(row, "known_benign")
            session.add(row)
            await session.flush()
            return _group_view(row)
    except IntegrityError as error:
        raise HTTPException(
            status_code=409, detail="generation changed concurrently"
        ) from error


@router.get("/groups/{group_id}", response_model=V13GenerationGroupView)
async def get_generation_group(
    group_id: UUID, _admin: AdminDep, session: SessionDep
) -> V13GenerationGroupView:
    """Read one digest-only generation start; never expose protected cases."""
    row = await session.get(V13PrivateGenerationGroup, group_id)
    if row is None:
        raise HTTPException(status_code=404, detail="generation group not found")
    return _group_view(row)
