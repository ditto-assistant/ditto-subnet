"""Append-only, digest-only V13 pre-randomness generation registry.

Recording a group is an audit event, not permission to issue seeds or clear a
hold. A protected provisioner must call the trusted-approval read, which
fails closed unless two distinct authenticated reviewers and the live image
still match. ``X-Admin-Actor`` never upgrades a recorded approval.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Annotated, Literal, cast
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.v13_private_generation import (
    V13GenerationGroupView,
    V13GenerationStartRequest,
    V13GroupPackageRegisterRequest,
    V13GroupPackageView,
    V13KnownBenignApprovalRequest,
    V13KnownBenignApprovalView,
    V13KnownBenignAttestationRequest,
    V13TrustedKnownBenignApproval,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.api_server.v13_benign_identity import verify_v13_benign_assertion
from ditto.api_server.v13_benign_provenance import (
    generator_conflicts,
    load_trusted_known_benign_approval,
)
from ditto.db.models import (
    Agent,
    ScreenedImageUpload,
    ScreeningAttempt,
    ScreeningPrivatePackageRegistration,
    V13GroupPackageRegistration,
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


@router.post(
    "/known-benign-approvals/{approval_id}/attest",
    response_model=V13KnownBenignApprovalView,
)
async def attest_known_benign_approval(
    approval_id: UUID,
    payload: V13KnownBenignAttestationRequest,
    request: Request,
    _admin: AdminDep,
    session: SessionDep,
) -> V13KnownBenignApprovalView:
    """Record one authenticated reviewer. The admin bearer cannot count."""
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
                action="attest-known-benign",
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
                return _approval_view(approval)
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
            return _approval_view(approval)
    except IntegrityError as error:
        raise HTTPException(
            status_code=409, detail="attestation changed concurrently"
        ) from error


@router.get(
    "/known-benign-approvals/{approval_id}/trusted",
    response_model=V13TrustedKnownBenignApproval,
)
async def get_trusted_known_benign_approval(
    approval_id: UUID, _admin: AdminDep, session: SessionDep
) -> V13TrustedKnownBenignApproval:
    """Verified two-person projection. A legacy row stays untrusted."""
    approval = await session.get(V13KnownBenignControlApproval, approval_id)
    if approval is None:
        raise HTTPException(status_code=404, detail="approval not found")
    return await load_trusted_known_benign_approval(session, approval)


@router.post(
    "/known-benign-approvals/{approval_id}/authorize-generation",
    response_model=V13TrustedKnownBenignApproval,
)
async def authorize_known_benign_generation(
    approval_id: UUID,
    payload: V13KnownBenignAttestationRequest,
    request: Request,
    _admin: AdminDep,
    session: SessionDep,
) -> V13TrustedKnownBenignApproval:
    """Allow generation only for a principal who did not approve the control."""
    approval = await session.get(V13KnownBenignControlApproval, approval_id)
    if approval is None:
        raise HTTPException(status_code=404, detail="approval not found")
    trusted = await load_trusted_known_benign_approval(session, approval)
    principal = verify_v13_benign_assertion(
        payload.assertion,
        secret=request.app.state.config.v13_benign_attestation_secret,
        approval_id=approval_id,
        evidence_sha256=approval.review_evidence_sha256,
        action="authorize-generation",
    )
    emails = set(
        await session.scalars(
            select(V13KnownBenignAttestation.principal_email).where(
                V13KnownBenignAttestation.approval_id == approval_id
            )
        )
    )
    if generator_conflicts(trusted, principal, reviewer_emails=emails):
        raise HTTPException(
            status_code=409,
            detail="generation principal also approved the control",
        )
    return trusted


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


def _package_view(
    group: V13PrivateGenerationGroup, row: V13GroupPackageRegistration
) -> V13GroupPackageView:
    if row.role not in {"target", "known_benign"}:
        raise HTTPException(status_code=503, detail="package role invalid")
    target = row.role == "target"
    return V13GroupPackageView(
        group_id=group.group_id,
        role=cast(Literal["target", "known_benign"], row.role),
        agent_id=group.target_agent_id if target else group.control_agent_id,
        attempt_id=group.target_attempt_id if target else group.control_attempt_id,
        artifact_sha256=(
            group.target_artifact_sha256 if target else group.control_artifact_sha256
        ),
        image_sha256=group.target_image_sha256
        if target
        else group.control_image_sha256,
        profile_sha256=group.profile_sha256,
        generation_receipt_sha256=row.generation_receipt_sha256,
        manifest_sha256=row.manifest_sha256,
        pair_inventory_sha256=row.pair_inventory_sha256,
        registrar_actor=row.registrar_actor,
        registered_at=row.registered_at,
    )


@router.post("/groups/{group_id}/packages/{role}", response_model=V13GroupPackageView)
async def register_group_package(
    group_id: UUID,
    role: Literal["target", "known_benign"],
    payload: V13GroupPackageRegisterRequest,
    _admin: AdminDep,
    session: SessionDep,
    x_admin_actor: Annotated[str | None, Header()] = None,
) -> V13GroupPackageView:
    """Record a digest-only package after a committed generation start.

    This does not authenticate the protected package or make an admin-supplied
    manifest a trusted matched control. A separate provisioner and verifier
    must validate sealed bytes and independent benign-control provenance.
    """
    actor = _actor(x_admin_actor)
    try:
        async with session.begin():
            # Serialize the two role inserts so pair-inventory equality cannot
            # be bypassed by concurrent first registrations.
            group = await session.get(
                V13PrivateGenerationGroup, group_id, with_for_update=True
            )
            if group is None:
                raise HTTPException(
                    status_code=404, detail="generation group not found"
                )
            expected_receipt = (
                group.target_receipt_sha256
                if role == "target"
                else group.control_receipt_sha256
            )
            if (
                payload.generation_receipt_sha256 != expected_receipt
                or expected_receipt != generation_role_digest(group, role)
            ):
                raise HTTPException(
                    status_code=409, detail="generation receipt mismatch"
                )
            target = await session.get(Agent, group.target_agent_id)
            control = await session.get(Agent, group.control_agent_id)
            target_attempt = await session.get(
                ScreeningAttempt, group.target_attempt_id
            )
            control_attempt = await session.get(
                ScreeningAttempt, group.control_attempt_id
            )
            if (
                target is None
                or control is None
                or target_attempt is None
                or control_attempt is None
                or target.status
                not in {AgentStatus.QUARANTINED, AgentStatus.ATH_PENDING_REVIEW}
                or control.status not in {AgentStatus.SCORED, AgentStatus.LIVE}
                or target.sha256.lower() != group.target_artifact_sha256
                or control.sha256.lower() != group.control_artifact_sha256
                or target_attempt.agent_id != group.target_agent_id
                or control_attempt.agent_id != group.control_agent_id
                or target_attempt.policy_version != 13
                or control_attempt.policy_version != 13
                or target_attempt.artifact_sha256 != group.target_artifact_sha256
                or control_attempt.artifact_sha256 != group.control_artifact_sha256
                or not await _bound_image(
                    session,
                    agent=target,
                    attempt=target_attempt,
                    image=group.target_image_sha256,
                )
                or not await _bound_image(
                    session,
                    agent=control,
                    attempt=control_attempt,
                    image=group.control_image_sha256,
                )
            ):
                raise HTTPException(status_code=409, detail="package identity changed")
            existing = await session.get(V13GroupPackageRegistration, (group_id, role))
            if existing is not None:
                if (
                    existing.generation_receipt_sha256
                    != payload.generation_receipt_sha256
                    or existing.manifest_sha256 != payload.manifest_sha256
                    or existing.pair_inventory_sha256 != payload.pair_inventory_sha256
                ):
                    raise HTTPException(status_code=409, detail="package conflicts")
                return _package_view(group, existing)
            other_role = "known_benign" if role == "target" else "target"
            other = await session.get(
                V13GroupPackageRegistration, (group_id, other_role)
            )
            if (
                other is not None
                and other.pair_inventory_sha256 != payload.pair_inventory_sha256
            ):
                raise HTTPException(status_code=409, detail="pair inventory differs")
            registered_at = await _database_now(session)
            if registered_at <= group.started_at:
                raise HTTPException(status_code=409, detail="package predates group")
            row = V13GroupPackageRegistration(
                group_id=group_id,
                role=role,
                generation_receipt_sha256=payload.generation_receipt_sha256,
                manifest_sha256=payload.manifest_sha256,
                pair_inventory_sha256=payload.pair_inventory_sha256,
                registrar_actor=actor,
                registered_at=registered_at,
            )
            session.add(row)
            await session.flush()
            return _package_view(group, row)
    except IntegrityError as error:
        raise HTTPException(
            status_code=409, detail="package changed concurrently"
        ) from error


@router.get("/groups/{group_id}/packages/{role}", response_model=V13GroupPackageView)
async def get_group_package(
    group_id: UUID,
    role: Literal["target", "known_benign"],
    _admin: AdminDep,
    session: SessionDep,
) -> V13GroupPackageView:
    """Read a group role's exact metadata; legacy attempt rows never qualify."""
    group = await session.get(V13PrivateGenerationGroup, group_id)
    row = await session.get(V13GroupPackageRegistration, (group_id, role))
    if group is None or row is None:
        raise HTTPException(status_code=404, detail="group package not found")
    return _package_view(group, row)
