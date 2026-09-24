"""Append-only, digest-only V13 pre-randomness generation registry.

Recording a group is an audit event, not permission to issue seeds or clear a
hold. A future protected provisioner must fetch and verify this committed
event before invoking CSPRNG or exposing any private case bytes.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Annotated, Literal, cast
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException
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
    V13ReplayGenerationGroupView,
    V13ReplayGroupPackageView,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.api_server.endpoints.verification_replay import _binding_ok
from ditto.db.models import (
    Agent,
    ScreenedImageUpload,
    ScreeningAttempt,
    ScreeningPrivatePackageRegistration,
    ScreeningVerificationReplay,
    V13GroupPackageRegistration,
    V13KnownBenignControlApproval,
    V13PrivateGenerationGroup,
    V13ReplayGroupPackageRegistration,
    V13ReplayPrivateGenerationGroup,
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


def replay_generation_role_digest(
    group: V13ReplayPrivateGenerationGroup, role: Literal["target", "known_benign"]
) -> str:
    """Domain-separated commitment to the independent replay and both roles."""
    return _canonical_digest(
        {
            "revision": "v13-replay-generation-group-v1",
            "group_id": str(group.group_id),
            "replay_id": str(group.replay_id),
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
            "approval_receipt_sha256": group.approval_receipt_sha256,
            "profile_sha256": group.profile_sha256,
            "started_at": _utc_stamp(group.started_at),
        }
    )


def _replay_group_view(
    row: V13ReplayPrivateGenerationGroup,
) -> V13ReplayGenerationGroupView:
    return V13ReplayGenerationGroupView.model_validate(
        {
            key: getattr(row, key)
            for key in V13ReplayGenerationGroupView.model_fields
            if key != "status"
        }
    )


def _replay_package_view(
    group: V13ReplayPrivateGenerationGroup,
    row: V13ReplayGroupPackageRegistration,
) -> V13ReplayGroupPackageView:
    target = row.role == "target"
    return V13ReplayGroupPackageView(
        replay_id=group.replay_id,
        group_id=group.group_id,
        role=cast(Literal["target", "known_benign"], row.role),
        agent_id=group.target_agent_id if target else group.control_agent_id,
        attempt_id=group.target_attempt_id if target else group.control_attempt_id,
        artifact_sha256=(
            group.target_artifact_sha256 if target else group.control_artifact_sha256
        ),
        image_sha256=(
            group.target_image_sha256 if target else group.control_image_sha256
        ),
        profile_sha256=group.profile_sha256,
        generation_receipt_sha256=row.generation_receipt_sha256,
        manifest_sha256=row.manifest_sha256,
        pair_inventory_sha256=row.pair_inventory_sha256,
        registrar_actor=row.registrar_actor,
        registered_at=row.registered_at,
    )


async def _replay_image_bound(
    session: AsyncSession,
    replay: ScreeningVerificationReplay,
    group: V13ReplayPrivateGenerationGroup | None = None,
) -> bool:
    now = await _database_now(session)
    return bool(
        replay.policy_version == 13
        and replay.image_upload_id is None
        and replay.status == "running"
        and replay.lease_deadline is not None
        and replay.lease_deadline > now
        and replay.image_verified_at is not None
        and replay.image_verified_storage_key is not None
        and replay.image_sha256 is not None
        and replay.image_id is not None
        and (group is None or replay.image_sha256 == group.target_image_sha256)
        and await _binding_ok(session, replay, lock=True)
    )


@router.post("/replays/{replay_id}/group", response_model=V13ReplayGenerationGroupView)
async def record_replay_generation_start(
    replay_id: UUID,
    payload: V13GenerationStartRequest,
    _admin: AdminDep,
    session: SessionDep,
    x_admin_actor: Annotated[str | None, Header()] = None,
) -> V13ReplayGenerationGroupView:
    """Commit replay image and clean control before protected seed generation."""
    actor = _actor(x_admin_actor)
    if payload.profile_sha256 != V13_PRIVATE_PROFILE_SHA256:
        raise HTTPException(409, "V13 profile mismatch")
    try:
        async with session.begin():
            replay = await session.get(
                ScreeningVerificationReplay, replay_id, with_for_update=True
            )
            if replay is None:
                raise HTTPException(404, "replay not found")
            if (
                replay.agent_id != payload.target_agent_id
                or replay.source_attempt_id != payload.target_attempt_id
                or replay.artifact_sha256 != payload.target_artifact_sha256
                or replay.image_sha256 != payload.target_image_sha256
                or not await _replay_image_bound(session, replay)
            ):
                raise HTTPException(409, "replay image guard mismatch")
            approval = await session.get(
                V13KnownBenignControlApproval, payload.approval_id
            )
            if approval is None:
                raise HTTPException(404, "clean approval not found")
            control = await session.get(Agent, approval.agent_id, with_for_update=True)
            control_attempt = await session.get(
                ScreeningAttempt, approval.attempt_id, with_for_update=True
            )
            if (
                control is None
                or control_attempt is None
                or control.agent_id == replay.agent_id
                or control.status not in {AgentStatus.SCORED, AgentStatus.LIVE}
                or control.sha256.lower() != approval.artifact_sha256
                or control_attempt.agent_id != control.agent_id
                or control_attempt.policy_version != 13
                or control_attempt.artifact_sha256 != approval.artifact_sha256
                or approval.profile_sha256 != payload.profile_sha256
                or approval.artifact_sha256 == replay.artifact_sha256
                or approval.image_sha256 == replay.image_sha256
                or not await _bound_image(
                    session,
                    agent=control,
                    attempt=control_attempt,
                    image=approval.image_sha256,
                )
            ):
                raise HTTPException(409, "clean control guard mismatch")
            existing = await session.scalar(
                select(V13ReplayPrivateGenerationGroup).where(
                    V13ReplayPrivateGenerationGroup.replay_id == replay_id
                )
            )
            if existing is not None:
                if (
                    existing.target_image_sha256 != payload.target_image_sha256
                    or existing.approval_id != approval.approval_id
                    or existing.profile_sha256 != payload.profile_sha256
                ):
                    raise HTTPException(409, "replay generation conflicts")
                return _replay_group_view(existing)
            started_at = await _database_now(session)
            if started_at <= max(approval.approved_at, replay.image_verified_at):
                raise HTTPException(
                    409, "approval or image verification not prior to start"
                )
            row = V13ReplayPrivateGenerationGroup(
                group_id=uuid4(),
                replay_id=replay_id,
                target_agent_id=replay.agent_id,
                target_attempt_id=replay.source_attempt_id,
                target_artifact_sha256=replay.artifact_sha256,
                target_image_sha256=replay.image_sha256,
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
            row.target_receipt_sha256 = replay_generation_role_digest(row, "target")
            row.control_receipt_sha256 = replay_generation_role_digest(
                row, "known_benign"
            )
            session.add(row)
            await session.flush()
            return _replay_group_view(row)
    except IntegrityError as error:
        raise HTTPException(409, "replay generation changed concurrently") from error


@router.get("/replays/{replay_id}/group", response_model=V13ReplayGenerationGroupView)
async def get_replay_generation_group(
    replay_id: UUID, _admin: AdminDep, session: SessionDep
) -> V13ReplayGenerationGroupView:
    row = await session.scalar(
        select(V13ReplayPrivateGenerationGroup).where(
            V13ReplayPrivateGenerationGroup.replay_id == replay_id
        )
    )
    if row is None:
        raise HTTPException(404, "replay generation group not found")
    return _replay_group_view(row)


@router.post(
    "/replays/{replay_id}/packages/{role}",
    response_model=V13ReplayGroupPackageView,
)
async def register_replay_group_package(
    replay_id: UUID,
    role: Literal["target", "known_benign"],
    payload: V13GroupPackageRegisterRequest,
    _admin: AdminDep,
    session: SessionDep,
    x_admin_actor: Annotated[str | None, Header()] = None,
) -> V13ReplayGroupPackageView:
    """Record sealed package digests; registration alone is not verification."""
    actor = _actor(x_admin_actor)
    try:
        async with session.begin():
            replay = await session.get(
                ScreeningVerificationReplay, replay_id, with_for_update=True
            )
            group = await session.scalar(
                select(V13ReplayPrivateGenerationGroup)
                .where(V13ReplayPrivateGenerationGroup.replay_id == replay_id)
                .with_for_update()
            )
            if replay is None or group is None:
                raise HTTPException(404, "replay generation group not found")
            expected = (
                group.target_receipt_sha256
                if role == "target"
                else group.control_receipt_sha256
            )
            if (
                payload.generation_receipt_sha256 != expected
                or expected != replay_generation_role_digest(group, role)
                or not await _replay_image_bound(session, replay, group)
            ):
                raise HTTPException(409, "replay generation binding mismatch")
            control = await session.get(Agent, group.control_agent_id)
            control_attempt = await session.get(
                ScreeningAttempt, group.control_attempt_id
            )
            approval = await session.get(
                V13KnownBenignControlApproval, group.approval_id
            )
            if (
                control is None
                or control_attempt is None
                or approval is None
                or control.status not in {AgentStatus.SCORED, AgentStatus.LIVE}
                or control.sha256.lower() != group.control_artifact_sha256
                or control_attempt.agent_id != group.control_agent_id
                or control_attempt.policy_version != 13
                or control_attempt.artifact_sha256 != group.control_artifact_sha256
                or approval.image_sha256 != group.control_image_sha256
                or approval.approval_receipt_sha256 != group.approval_receipt_sha256
                or not await _bound_image(
                    session,
                    agent=control,
                    attempt=control_attempt,
                    image=group.control_image_sha256,
                )
            ):
                raise HTTPException(409, "clean control identity changed")
            existing = await session.get(
                V13ReplayGroupPackageRegistration, (group.group_id, role)
            )
            if existing is not None:
                if (
                    existing.generation_receipt_sha256 != expected
                    or existing.manifest_sha256 != payload.manifest_sha256
                    or existing.pair_inventory_sha256 != payload.pair_inventory_sha256
                ):
                    raise HTTPException(409, "replay package conflicts")
                return _replay_package_view(group, existing)
            other_role = "known_benign" if role == "target" else "target"
            other = await session.get(
                V13ReplayGroupPackageRegistration, (group.group_id, other_role)
            )
            if (
                other is not None
                and other.pair_inventory_sha256 != payload.pair_inventory_sha256
            ):
                raise HTTPException(409, "pair inventory differs")
            registered_at = await _database_now(session)
            if registered_at <= group.started_at:
                raise HTTPException(409, "package predates generation group")
            row = V13ReplayGroupPackageRegistration(
                group_id=group.group_id,
                role=role,
                generation_receipt_sha256=expected,
                manifest_sha256=payload.manifest_sha256,
                pair_inventory_sha256=payload.pair_inventory_sha256,
                registrar_actor=actor,
                registered_at=registered_at,
            )
            session.add(row)
            await session.flush()
            return _replay_package_view(group, row)
    except IntegrityError as error:
        raise HTTPException(409, "replay package changed concurrently") from error


@router.get(
    "/replays/{replay_id}/packages/{role}",
    response_model=V13ReplayGroupPackageView,
)
async def get_replay_group_package(
    replay_id: UUID,
    role: Literal["target", "known_benign"],
    _admin: AdminDep,
    session: SessionDep,
) -> V13ReplayGroupPackageView:
    group = await session.scalar(
        select(V13ReplayPrivateGenerationGroup).where(
            V13ReplayPrivateGenerationGroup.replay_id == replay_id
        )
    )
    if group is None:
        raise HTTPException(404, "replay generation group not found")
    row = await session.get(V13ReplayGroupPackageRegistration, (group.group_id, role))
    if row is None:
        raise HTTPException(404, "replay group package not found")
    return _replay_package_view(group, row)
