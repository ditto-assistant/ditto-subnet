"""Independent, report-only V13 source-verification replay leases.

This is a transport and evidence ledger, not a verification runner or a CLEAR
path. In particular, replay receipts do not satisfy the mandatory V13 profile.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal, cast
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.system_health import fleet_release_from_heartbeat_envelope
from ditto.api_models.verification_replay import (
    VerificationReplayBuildUpload,
    VerificationReplayBuildUploadRequest,
    VerificationReplayBuildVerifyRequest,
    VerificationReplayClaimability,
    VerificationReplayCreate,
    VerificationReplayFinish,
    VerificationReplayInputs,
    VerificationReplayReceiptRequest,
    VerificationReplayReceiptState,
    VerificationReplayState,
)
from ditto.api_server.dependencies import get_session, get_storage_client
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.api_server.endpoints.admin_screener_capacity import (
    _MIN_VERIFICATION_REPLAY_RUNNER_RELEASE,
    _replay_workers_ready,
)
from ditto.api_server.endpoints.screener import (
    ScreenerDep,
    _artifact_key,
    _screened_image_key,
)
from ditto.api_server.v13_replay_process_identity import (
    ReplayProcessProofError,
    ReplayProcessRegistration,
    VerifiedReplayProcessHeartbeat,
    verify_replay_process_proof,
)
from ditto.db.models import (
    Agent,
    ScreenedImageUpload,
    ScreenerCapacityEvent,
    ScreenerHeartbeat,
    ScreenerNode,
    ScreenerReplayProcessKey,
    ScreenerReplayProcessNonce,
    ScreeningAttempt,
    ScreeningQuarantine,
    ScreeningVerificationReplay,
    ScreeningVerificationReplayReceipt,
)
from ditto_screening_protocol.v13_replay_process_identity import (
    ReplayPurpose,
    V13ReplayProcessProof,
)

admin_router = APIRouter(prefix="/admin/screening-verification-replays", tags=["admin"])
screener_router = APIRouter(prefix="/screener/verification-replays", tags=["screener"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]
LEASE = timedelta(minutes=30)
MAX_REPLAY_LEASE = timedelta(hours=4)
MAX_REPLAY_RENEWALS = 8
RENEW_WINDOW = timedelta(minutes=10)
URL_TTL_SECONDS = 300


class ReplayProcessKeyWrite(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    expected_hotkey: str
    instance_id: str = Field(min_length=1, max_length=63)
    public_key_hex: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: str = Field(min_length=8)
    confirmation: str


class ReplayProcessKeyRevoke(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    expected_hotkey: str
    expected_key_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: str = Field(min_length=8)
    confirmation: str


def _process_instance(node_id: str, instance_id: str) -> bool:
    return node_id == "subnet-screener-2" and instance_id == f"{node_id}-worker-1"


@admin_router.post("/process-keys/{node_id}", status_code=204)
async def register_replay_process_key(
    node_id: str,
    payload: ReplayProcessKeyWrite,
    _admin: AdminDep,
    session: SessionDep,
    x_admin_actor: Annotated[str | None, Header()] = None,
) -> None:
    """Pin one host-generated worker public key; never accept a node bearer."""

    actor = x_admin_actor.strip() if x_admin_actor else ""
    if not 1 <= len(actor) <= 120 or not _process_instance(
        node_id, payload.instance_id
    ):
        raise HTTPException(400, "invalid replay process enrollment identity")
    key_sha256 = hashlib.sha256(bytes.fromhex(payload.public_key_hex)).hexdigest()
    expected = (
        f"REGISTER V13 REPLAY PROCESS {node_id}/{payload.instance_id}/{key_sha256}"
    )
    if payload.confirmation != expected:
        raise HTTPException(409, f"confirmation must be exactly {expected}")
    now = datetime.now(UTC)
    async with session.begin():
        node = await session.get(ScreenerNode, node_id, with_for_update=True)
        if (
            node is None
            or node.environment != "prod"
            or node.provider != "hetzner"
            or node.status != "active"
            or node.screener_hotkey != payload.expected_hotkey
            or node.verification_replay_capacity != 0
        ):
            raise HTTPException(409, "independent replay node state changed")
        existing = await session.scalar(
            select(ScreenerReplayProcessKey).where(
                ScreenerReplayProcessKey.node_id == node_id,
                ScreenerReplayProcessKey.instance_id == payload.instance_id,
                ScreenerReplayProcessKey.status == "active",
            )
        )
        if existing is not None:
            raise HTTPException(409, "active replay process key already registered")
        if await session.get(ScreenerReplayProcessKey, key_sha256) is not None:
            raise HTTPException(409, "replay process key was already registered")
        prior_revision = await session.scalar(
            select(func.max(ScreenerReplayProcessKey.revision)).where(
                ScreenerReplayProcessKey.node_id == node_id,
                ScreenerReplayProcessKey.instance_id == payload.instance_id,
            )
        )
        session.add(
            ScreenerReplayProcessKey(
                node_id=node_id,
                instance_id=payload.instance_id,
                public_key_hex=payload.public_key_hex,
                key_sha256=key_sha256,
                revision=(prior_revision or 0) + 1,
                status="active",
                registered_at=now,
            )
        )
        session.add(
            ScreenerCapacityEvent(
                event_id=uuid4(),
                environment="prod",
                event_type="replay_key_registered",
                provider="hetzner",
                node_id=node_id,
                detail=(
                    f"instance={payload.instance_id} key={key_sha256} "
                    f"actor={actor} reason={payload.reason}"
                ),
                controller_epoch="backroom-replay-control",
                created_at=now,
            )
        )


@admin_router.post("/process-keys/{node_id}/revoke", status_code=204)
async def revoke_replay_process_key(
    node_id: str,
    payload: ReplayProcessKeyRevoke,
    _admin: AdminDep,
    session: SessionDep,
    x_admin_actor: Annotated[str | None, Header()] = None,
) -> None:
    """Revoke one exact key while keeping its nonce and audit history."""

    actor = x_admin_actor.strip() if x_admin_actor else ""
    if not 1 <= len(actor) <= 120 or node_id != "subnet-screener-2":
        raise HTTPException(400, "invalid replay process revocation identity")
    expected = f"REVOKE V13 REPLAY PROCESS {node_id}/{payload.expected_key_sha256}"
    if payload.confirmation != expected:
        raise HTTPException(409, f"confirmation must be exactly {expected}")
    now = datetime.now(UTC)
    async with session.begin():
        node = await session.get(ScreenerNode, node_id, with_for_update=True)
        key = await session.get(
            ScreenerReplayProcessKey, payload.expected_key_sha256, with_for_update=True
        )
        if (
            node is None
            or node.screener_hotkey != payload.expected_hotkey
            or key is None
            or key.node_id != node_id
            or key.status != "active"
        ):
            raise HTTPException(409, "replay process key or node state changed")
        key.status = "revoked"
        key.revoked_at = now
        session.add(
            ScreenerCapacityEvent(
                event_id=uuid4(),
                environment="prod",
                event_type="replay_key_revoked",
                provider="hetzner",
                node_id=node_id,
                detail=(
                    f"instance={key.instance_id} key={key.key_sha256} "
                    f"actor={actor} reason={payload.reason}"
                ),
                controller_epoch="backroom-replay-control",
                created_at=now,
            )
        )


async def verify_replay_process_request(
    request: Request,
    session: AsyncSession,
    *,
    node: ScreenerNode,
    instance_id: str,
    purpose: ReplayPurpose,
    expected_path: str | None = None,
    expected_key_sha256: str | None = None,
) -> str:
    """Verify a signed process proof and consume its nonce in this transaction."""

    raw = request.headers.get("x-replay-process-proof", "")
    signature = request.headers.get("x-replay-process-signature", "")
    if not raw or len(raw) > 1024 or not signature or len(signature) > 128:
        raise HTTPException(403, "missing replay process proof")
    try:
        proof = V13ReplayProcessProof.model_validate_json(raw)
    except ValidationError as exc:
        raise HTTPException(403, "invalid replay process proof") from exc
    key = await session.scalar(
        select(ScreenerReplayProcessKey)
        .where(
            ScreenerReplayProcessKey.node_id == node.node_id,
            ScreenerReplayProcessKey.instance_id == instance_id,
            ScreenerReplayProcessKey.status == "active",
        )
        .with_for_update()
    )
    if key is None:
        raise HTTPException(403, "replay process key not registered")
    if expected_key_sha256 is not None and key.key_sha256 != expected_key_sha256:
        raise HTTPException(403, "replay lease belongs to another process key")
    heartbeat: VerifiedReplayProcessHeartbeat | None = None
    if purpose == "claim":
        row = await session.get(
            ScreenerHeartbeat,
            (node.screener_hotkey, instance_id),
            with_for_update=True,
            populate_existing=True,
        )
        envelope = row.system_metrics if row is not None else None
        verified = (
            envelope.get("replay_process") if isinstance(envelope, dict) else None
        )
        release = (
            fleet_release_from_heartbeat_envelope(envelope)
            if isinstance(envelope, dict)
            else None
        )
        if (
            row is not None
            and isinstance(verified, dict)
            and release is not None
            and release.version is not None
        ):
            parts = release.version.removeprefix("v").split(".")
            if len(parts) == 3 and all(part.isdigit() for part in parts):
                heartbeat = VerifiedReplayProcessHeartbeat(
                    node_id=node.node_id,
                    instance_id=instance_id,
                    key_sha256=str(verified.get("key_sha256", "")),
                    seen_at=int(row.seen_at.timestamp()),
                    policy_version=row.policy_version,
                    release=(int(parts[0]), int(parts[1]), int(parts[2])),
                )

    async def consume_nonce(node_id: str, claimant_instance: str, nonce: str) -> bool:
        if node_id != node.node_id or claimant_instance != instance_id:
            return False
        try:
            async with session.begin_nested():
                session.add(
                    ScreenerReplayProcessNonce(
                        key_sha256=key.key_sha256,
                        nonce=nonce,
                        consumed_at=datetime.now(UTC),
                    )
                )
                await session.flush()
        except IntegrityError:
            return False
        return True

    try:
        await verify_replay_process_proof(
            proof=proof,
            signature_hex=signature,
            registration=ReplayProcessRegistration(
                node_id=node.node_id,
                instance_id=instance_id,
                public_key_hex=key.public_key_hex,
                revoked=key.status != "active",
            ),
            authenticated_node_id=node.node_id,
            purpose=purpose,
            body=await request.body(),
            method=getattr(request, "method", "POST"),
            path=expected_path,
            now=int(datetime.now(UTC).timestamp()),
            consume_nonce=consume_nonce,
            heartbeat=heartbeat,
            minimum_release=_MIN_VERIFICATION_REPLAY_RUNNER_RELEASE,
        )
    except ReplayProcessProofError as exc:
        raise HTTPException(403, str(exc)) from exc
    return key.key_sha256


def _replay_staging_image_key(replay_id: UUID, staging_id: UUID) -> str:
    return f"verification-replays/{replay_id}/staging/{staging_id}.tar"


def _replay_verified_image_key(replay_id: UUID, candidate_id: UUID) -> str:
    return f"verification-replays/{replay_id}/verified/{candidate_id}.tar"


async def _require_enrolled_replay_worker(
    request: Request, worker: str, session: AsyncSession
) -> ScreenerNode:
    """Legacy fleet tokens and non-prod nodes are not replay authority."""
    node_id = getattr(request.state, "screener_node_id", None)
    if node_id is None or request.state.screener_node_status != "active":
        raise HTTPException(403, "independent enrolled screener node required")
    node = await session.get(
        ScreenerNode, node_id, populate_existing=True, with_for_update=True
    )
    if (
        node is None
        or node.status != "active"
        or node.environment != "prod"
        or node.screener_hotkey != worker
        or node.token_expires_at <= datetime.now(UTC)
        or node.verification_replay_capacity <= 0
    ):
        raise HTTPException(403, "independent enrolled screener node required")
    return node


def _state(
    row: ScreeningVerificationReplay, receipt_count: int = 0
) -> VerificationReplayState:
    return VerificationReplayState(
        replay_id=row.replay_id,
        request_id=row.request_id,
        agent_id=row.agent_id,
        quarantine_id=row.quarantine_id,
        source_attempt_id=row.source_attempt_id,
        artifact_sha256=row.artifact_sha256,
        policy_version=row.policy_version,
        image_upload_id=row.image_upload_id,
        image_sha256=row.image_sha256,
        image_size_bytes=row.image_size_bytes,
        image_id=row.image_id,
        image_staging_id=row.image_staging_id,
        image_verified_at=row.image_verified_at,
        image_verified_storage_key=row.image_verified_storage_key,
        status=cast(Literal["queued", "running", "reported", "failed"], row.status),
        worker_hotkey=row.worker_hotkey,
        lease_deadline=row.lease_deadline,
        lease_started_at=row.lease_started_at,
        lease_renewals=row.lease_renewals,
        created_at=row.created_at,
        finished_at=row.finished_at,
        failure_code=row.failure_code,
        receipt_count=receipt_count,
    )


async def _binding_ok(
    session: AsyncSession, row: ScreeningVerificationReplay, *, lock: bool = False
) -> bool:
    # Lifecycle writers lock Agent first. Keep this order in every replay
    # mutation so an operator resolution cannot race the evidence check.
    agent = await session.get(
        Agent, row.agent_id, populate_existing=True, with_for_update=lock
    )
    attempt = await session.get(
        ScreeningAttempt,
        row.source_attempt_id,
        populate_existing=True,
        with_for_update=lock,
    )
    quarantine = await session.get(
        ScreeningQuarantine,
        row.quarantine_id,
        populate_existing=True,
        with_for_update=lock,
    )
    image = (
        await session.get(
            ScreenedImageUpload,
            row.image_upload_id,
            populate_existing=True,
            with_for_update=lock,
        )
        if row.image_upload_id is not None
        else None
    )
    return bool(
        agent is not None
        and agent.status == "quarantined"
        and agent.sha256 == row.artifact_sha256
        and quarantine is not None
        and quarantine.agent_id == row.agent_id
        and quarantine.attempt_id == row.source_attempt_id
        and quarantine.policy_version == row.policy_version
        and quarantine.status == "active"
        and attempt is not None
        and attempt.agent_id == row.agent_id
        and attempt.status == "quarantined"
        and attempt.policy_version == row.policy_version
        and attempt.artifact_sha256 == row.artifact_sha256
        and (
            row.image_upload_id is None
            or (
                image is not None
                and image.agent_id == row.agent_id
                and image.status == "verified"
                and image.sha256 == row.image_sha256
                and image.verified_at is not None
                and agent.screened_image_upload_id == row.image_upload_id
                and agent.screened_image_sha256 == row.image_sha256
                and agent.screened_image_verified_at is not None
            )
        )
    )


def _same_request(
    row: ScreeningVerificationReplay, payload: VerificationReplayCreate
) -> bool:
    return (
        row.request_id == payload.request_id
        and row.quarantine_id == payload.quarantine_id
        and row.source_attempt_id == payload.source_attempt_id
        and row.artifact_sha256 == payload.artifact_sha256
        and row.policy_version == payload.policy_version
        and row.image_upload_id == payload.image_upload_id
        and row.image_sha256 == payload.image_sha256
        and row.actor == payload.actor
        and row.reason == payload.reason
    )


@admin_router.post("/{agent_id}", response_model=VerificationReplayState)
async def create_replay(
    agent_id: UUID,
    payload: VerificationReplayCreate,
    _admin: AdminDep,
    session: SessionDep,
) -> VerificationReplayState:
    prior = await session.scalar(
        select(ScreeningVerificationReplay).where(
            ScreeningVerificationReplay.request_id == payload.request_id
        )
    )
    if prior is not None:
        if prior.agent_id != agent_id or not _same_request(prior, payload):
            raise HTTPException(409, "replay request ID belongs to another binding")
        return _state(prior)
    # Lock the agent so a concurrent lifecycle/image change cannot pass guards
    # between the initial read and lease creation. The partial unique index is
    # the final guard against two administrators racing this request.
    agent = await session.scalar(
        select(Agent).where(Agent.agent_id == agent_id).with_for_update()
    )
    if agent is None:
        raise HTTPException(404, "agent not found")
    if (
        agent.status != payload.expected_agent_status
        or agent.sha256 != payload.artifact_sha256
    ):
        raise HTTPException(409, "agent status or artifact changed")
    row = ScreeningVerificationReplay(
        replay_id=uuid4(),
        request_id=payload.request_id,
        agent_id=agent_id,
        quarantine_id=payload.quarantine_id,
        source_attempt_id=payload.source_attempt_id,
        artifact_sha256=payload.artifact_sha256,
        policy_version=payload.policy_version,
        image_upload_id=payload.image_upload_id,
        image_sha256=payload.image_sha256,
        image_size_bytes=(
            agent.screened_image_size_bytes if payload.image_upload_id else None
        ),
        image_id=(agent.screened_image_id if payload.image_upload_id else None),
        image_verified_at=(
            agent.screened_image_verified_at if payload.image_upload_id else None
        ),
        image_verified_storage_key=None,
        status="queued",
        actor=payload.actor,
        reason=payload.reason,
        created_at=datetime.now(UTC),
    )
    if not await _binding_ok(session, row, lock=True):
        raise HTTPException(
            409, "source quarantine, pinned attempt, or optional verified image changed"
        )
    existing = await session.scalar(
        select(ScreeningVerificationReplay)
        .where(
            ScreeningVerificationReplay.agent_id == agent_id,
            ScreeningVerificationReplay.source_attempt_id == payload.source_attempt_id,
            ScreeningVerificationReplay.status.in_(("queued", "running")),
        )
        .with_for_update()
    )
    if existing is not None:
        if not _same_request(existing, payload):
            raise HTTPException(409, "active replay has different evidence binding")
        if not await _binding_ok(session, existing, lock=True):
            raise HTTPException(409, "active replay evidence binding changed")
        return _state(existing)
    session.add(row)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        prior = await session.scalar(
            select(ScreeningVerificationReplay).where(
                ScreeningVerificationReplay.request_id == payload.request_id
            )
        )
        if (
            prior is not None
            and prior.agent_id == agent_id
            and _same_request(prior, payload)
        ):
            return _state(prior)
        raise HTTPException(409, "replay already active") from exc
    return _state(row)


@admin_router.get("/{agent_id}/{replay_id}", response_model=VerificationReplayState)
async def get_replay(
    agent_id: UUID, replay_id: UUID, _admin: AdminDep, session: SessionDep
) -> VerificationReplayState:
    row = await session.get(ScreeningVerificationReplay, replay_id)
    if row is None or row.agent_id != agent_id:
        raise HTTPException(404, "replay not found")
    count = await session.scalar(
        select(func.count()).where(
            ScreeningVerificationReplayReceipt.replay_id == replay_id
        )
    )
    return _state(row, count or 0)


@admin_router.get(
    "/{agent_id}/{replay_id}/claimability",
    response_model=VerificationReplayClaimability,
)
async def get_replay_claimability(
    agent_id: UUID, replay_id: UUID, _admin: AdminDep, session: SessionDep
) -> VerificationReplayClaimability:
    """Show whether an independent enrolled identity exists, not worker readiness."""
    row = await session.get(ScreeningVerificationReplay, replay_id)
    if row is None or row.agent_id != agent_id:
        raise HTTPException(404, "replay not found")
    attempt = await session.get(ScreeningAttempt, row.source_attempt_id)
    if attempt is None or not attempt.screener_hotkey:
        raise HTTPException(409, "original screener identity unavailable")
    now = datetime.now(UTC)
    eligible = list(
        (
            await session.scalars(
                select(ScreenerNode.screener_hotkey)
                .where(
                    ScreenerNode.status == "active",
                    ScreenerNode.environment == "prod",
                    ScreenerNode.token_expires_at > now,
                    ScreenerNode.verification_replay_capacity > 0,
                    ScreenerNode.screener_hotkey != attempt.screener_hotkey,
                )
                .order_by(ScreenerNode.screener_hotkey)
            )
        ).all()
    )
    bound = await _binding_ok(session, row)
    return VerificationReplayClaimability(
        replay_id=replay_id,
        original_screener_hotkey=attempt.screener_hotkey,
        source_binding_current=bound,
        replay_enabled_independent_hotkeys=eligible,
        independent_replay_enabled=bool(eligible),
        note=(
            "Replay enablement does not prove a deployed or healthy worker; "
            "this replay remains report-only and cannot clear a hold."
        ),
    )


async def _active_claim(
    session: AsyncSession, replay_id: UUID, worker: str
) -> ScreeningVerificationReplay:
    row = await session.get(ScreeningVerificationReplay, replay_id)
    if row is None or row.worker_hotkey != worker:
        raise HTTPException(403, "replay lease belongs to another worker")
    if not await _binding_ok(session, row, lock=True):
        raise HTTPException(409, "replay evidence binding changed")
    row = await session.get(
        ScreeningVerificationReplay,
        replay_id,
        with_for_update=True,
        populate_existing=True,
    )
    if row is None or row.worker_hotkey != worker:
        raise HTTPException(403, "replay lease belongs to another worker")
    now = datetime.now(UTC)
    if (
        row.status != "running"
        or row.lease_deadline is None
        or row.lease_deadline <= now
    ):
        raise HTTPException(409, "replay lease is not active")
    if not await _binding_ok(session, row, lock=True):
        raise HTTPException(409, "replay evidence binding changed")
    return row


async def _require_lease_process(
    request: Request,
    session: AsyncSession,
    node: ScreenerNode,
    row: ScreeningVerificationReplay,
    purpose: ReplayPurpose,
) -> None:
    """Bind every lease API to the exact key that signed the original claim."""
    if (
        node.node_id != "subnet-screener-2"
        and _MIN_VERIFICATION_REPLAY_RUNNER_RELEASE is None
    ):
        return  # Legacy/default-off tests and existing node 1; no replay activation.
    if (
        node.node_id != "subnet-screener-2"
        or _MIN_VERIFICATION_REPLAY_RUNNER_RELEASE is None
    ):
        raise HTTPException(403, "independent replay process unavailable")
    instance_id = request.headers.get("x-replay-process-instance", "")
    if not _process_instance(node.node_id, instance_id):
        raise HTTPException(403, "replay process instance unavailable")
    if row.process_key_sha256 is None:
        raise HTTPException(403, "replay lease has no process key")
    await verify_replay_process_request(
        request,
        session,
        node=node,
        instance_id=instance_id,
        purpose=purpose,
        expected_path=f"/screener/verification-replays/{row.replay_id}/{purpose}",
        expected_key_sha256=row.process_key_sha256,
    )


@screener_router.post("/claim", response_model=VerificationReplayState | None)
async def claim_replay(
    request: Request, worker: ScreenerDep, session: SessionDep
) -> VerificationReplayState | None:
    node = await _require_enrolled_replay_worker(request, worker, session)
    process_key_sha256 = None
    if (
        node.node_id == "subnet-screener-2"
        or _MIN_VERIFICATION_REPLAY_RUNNER_RELEASE is not None
    ):
        if (
            node.node_id != "subnet-screener-2"
            or _MIN_VERIFICATION_REPLAY_RUNNER_RELEASE is None
        ):
            raise HTTPException(403, "independent replay process unavailable")
        instance_id = request.headers.get("x-replay-process-instance", "")
        if not _process_instance(node.node_id, instance_id):
            raise HTTPException(403, "replay process instance unavailable")
        process_key_sha256 = await verify_replay_process_request(
            request,
            session,
            node=node,
            instance_id=instance_id,
            purpose="claim",
            expected_path="/screener/verification-replays/claim",
        )
    now = datetime.now(UTC)
    # The capacity grant is durable, while worker health and release adoption
    # can change after it is enabled. Recheck before issuing every new lease.
    if not await _replay_workers_ready(session, node=node, now=now):
        raise HTTPException(409, "fresh v13 replay-runner workers unavailable")
    active = await session.scalar(
        select(func.count()).where(
            ScreeningVerificationReplay.worker_hotkey == worker,
            ScreeningVerificationReplay.status == "running",
            ScreeningVerificationReplay.lease_deadline > now,
        )
    )
    if (active or 0) >= node.verification_replay_capacity:
        return None
    # An expired lease never silently re-enters the queue. Take the same
    # source-first locks as every other replay writer before terminalizing it.
    expired = (
        await session.scalars(
            select(ScreeningVerificationReplay)
            .where(
                ScreeningVerificationReplay.status == "running",
                ScreeningVerificationReplay.lease_deadline <= now,
            )
            .order_by(ScreeningVerificationReplay.lease_deadline)
            .limit(8)
        )
    ).all()
    for candidate in expired:
        await _binding_ok(session, candidate, lock=True)
        current = await session.get(
            ScreeningVerificationReplay,
            candidate.replay_id,
            with_for_update=True,
            populate_existing=True,
        )
        if (
            current is not None
            and current.status == "running"
            and current.lease_deadline is not None
            and current.lease_deadline <= now
        ):
            current.status = "failed"
            current.finished_at = now
            current.failure_code = "lease-expired"
    rows = (
        await session.scalars(
            select(ScreeningVerificationReplay)
            .join(
                ScreeningAttempt,
                ScreeningAttempt.attempt_id
                == ScreeningVerificationReplay.source_attempt_id,
            )
            .where(
                ScreeningVerificationReplay.status == "queued",
                ScreeningAttempt.screener_hotkey != worker,
            )
            .order_by(
                ScreeningVerificationReplay.created_at,
                ScreeningVerificationReplay.replay_id,
            )
            .limit(8)
        )
    ).all()
    for row in rows:
        binding_ok = await _binding_ok(session, row, lock=True)
        locked_row = await session.get(
            ScreeningVerificationReplay,
            row.replay_id,
            with_for_update=True,
            populate_existing=True,
        )
        if locked_row is None or locked_row.status != "queued":
            continue
        row = locked_row
        if not binding_ok or not await _binding_ok(session, row, lock=True):
            row.status = "failed"
            row.finished_at = now
            row.failure_code = "binding-stale"
            continue
        row.status = "running"
        row.worker_hotkey = worker
        row.process_key_sha256 = process_key_sha256
        row.lease_started_at = now
        row.lease_deadline = now + LEASE
        await session.commit()
        return _state(row)
    await session.commit()
    return None


@screener_router.post("/{replay_id}/renew", response_model=VerificationReplayState)
async def renew_replay(
    replay_id: UUID, request: Request, worker: ScreenerDep, session: SessionDep
) -> VerificationReplayState:
    """Renew one exact active lease within an absolute four-hour worker budget.

    Renewal repeats the enrolled-node and source-binding checks and cannot
    resurrect an expired, settled, or rebound source hold. A capped renewal
    prevents a worker from keeping a quarantine pinned indefinitely.
    """
    node = await _require_enrolled_replay_worker(request, worker, session)
    row = await _active_claim(session, replay_id, worker)
    await _require_lease_process(request, session, node, row, "renew")
    now = datetime.now(UTC)
    if row.lease_started_at is None:
        raise HTTPException(409, "replay lease start is unavailable")
    if row.lease_renewals >= MAX_REPLAY_RENEWALS:
        raise HTTPException(409, "replay renewal budget exhausted")
    if row.lease_deadline is None or row.lease_deadline - now > RENEW_WINDOW:
        raise HTTPException(409, "replay lease is not in renewal window")
    max_deadline = row.lease_started_at + MAX_REPLAY_LEASE
    deadline = min(now + LEASE, max_deadline)
    if deadline <= row.lease_deadline:
        raise HTTPException(409, "replay absolute deadline reached")
    row.lease_deadline = deadline
    row.lease_renewals += 1
    await session.commit()
    return _state(row)


@screener_router.get("/{replay_id}/inputs", response_model=VerificationReplayInputs)
async def get_replay_inputs(
    replay_id: UUID, request: Request, worker: ScreenerDep, session: SessionDep
) -> VerificationReplayInputs:
    node = await _require_enrolled_replay_worker(request, worker, session)
    row = await _active_claim(session, replay_id, worker)
    await _require_lease_process(request, session, node, row, "inputs")
    storage = await get_storage_client(request)
    artifact_url = await storage.presigned_get_url(
        key=_artifact_key(row.agent_id), expires_in=URL_TTL_SECONDS
    )
    image_url = None
    if row.image_verified_at is not None:
        image_key = (
            _screened_image_key(row.agent_id, row.image_upload_id)
            if row.image_upload_id is not None
            else row.image_verified_storage_key
        )
        if image_key is None:
            raise HTTPException(409, "verified replay image key is unavailable")
        image_url = await storage.presigned_get_url(
            key=image_key, expires_in=URL_TTL_SECONDS
        )
    result = VerificationReplayInputs(
        replay=_state(row),
        artifact_url=artifact_url,
        image_url=image_url,
        urls_expire_at=datetime.now(UTC) + timedelta(seconds=URL_TTL_SECONDS),
    )
    await session.commit()
    return result


@screener_router.post(
    "/{replay_id}/build-upload", response_model=VerificationReplayBuildUpload
)
async def mint_replay_build_upload(
    replay_id: UUID,
    payload: VerificationReplayBuildUploadRequest,
    request: Request,
    worker: ScreenerDep,
    session: SessionDep,
) -> VerificationReplayBuildUpload:
    """Mint a short-lived PUT for a new isolated build, never the Agent image key."""
    node = await _require_enrolled_replay_worker(request, worker, session)
    row = await _active_claim(session, replay_id, worker)
    await _require_lease_process(request, session, node, row, "build-upload")
    if payload.artifact_sha256 != row.artifact_sha256:
        raise HTTPException(409, "replay artifact changed")
    if row.image_upload_id is not None or row.image_verified_at is not None:
        raise HTTPException(409, "replay already has a verified image")
    if row.image_sha256 is None:
        row.image_sha256 = payload.image_sha256
        row.image_size_bytes = payload.size_bytes
        row.image_id = payload.image_id
        row.image_staging_id = uuid4()
    elif (
        row.image_sha256 != payload.image_sha256
        or row.image_size_bytes != payload.size_bytes
        or row.image_id != payload.image_id
        or row.image_staging_id is None
    ):
        raise HTTPException(409, "build upload metadata already pinned differently")
    await session.commit()
    metadata = {
        "artifact-sha256": row.artifact_sha256,
        "image-sha256": row.image_sha256,
        "image-id": row.image_id,
        "replay-id": str(row.replay_id),
    }
    storage = await get_storage_client(request)
    url = await storage.presigned_put_url(
        key=_replay_staging_image_key(row.replay_id, row.image_staging_id),
        size_bytes=row.image_size_bytes,
        metadata=metadata,
        expires_in=URL_TTL_SECONDS,
    )
    return VerificationReplayBuildUpload(
        replay_id=row.replay_id,
        upload_url=url,
        required_headers={
            "Content-Length": str(row.image_size_bytes),
            "Content-Type": "application/x-tar",
            **{f"x-amz-meta-{key}": value for key, value in metadata.items()},
        },
        expires_at=datetime.now(UTC) + timedelta(seconds=URL_TTL_SECONDS),
    )


@screener_router.post(
    "/{replay_id}/build-verify", response_model=VerificationReplayState
)
async def verify_replay_build(
    replay_id: UUID,
    payload: VerificationReplayBuildVerifyRequest,
    request: Request,
    worker: ScreenerDep,
    session: SessionDep,
) -> VerificationReplayState:
    """Verify tar bytes, copy to a worker-unwritable key, and verify again.

    This does not prove the worker-claimed image ID inside the tar. The future
    isolated runner must load the image and compare its actual identity before
    recording V13 runtime observations.
    """
    node = await _require_enrolled_replay_worker(request, worker, session)
    row = await _active_claim(session, replay_id, worker)
    await _require_lease_process(request, session, node, row, "build-verify")
    if (
        row.image_upload_id is not None
        or row.image_staging_id is None
        or row.image_sha256 != payload.image_sha256
        or row.image_size_bytes != payload.size_bytes
        or row.image_id != payload.image_id
        or row.artifact_sha256 != payload.artifact_sha256
    ):
        raise HTTPException(409, "replay build binding changed")
    if row.image_verified_at is not None:
        return _state(row)
    metadata = {
        "artifact-sha256": row.artifact_sha256,
        "image-sha256": row.image_sha256,
        "image-id": row.image_id,
        "replay-id": str(row.replay_id),
    }
    staging_key = _replay_staging_image_key(row.replay_id, row.image_staging_id)
    # Every verification gets a distinct final key. A concurrent verification
    # may complete after this one, but cannot overwrite the image we pin in DB.
    final_key = _replay_verified_image_key(row.replay_id, uuid4())
    # Hashing a multi-GB tar can take time. Do not hold the submission's
    # lifecycle locks through object storage I/O; reacquire all source locks
    # and recheck the exact binding before recording a verified image.
    await session.commit()
    storage = await get_storage_client(request)
    try:
        staged = await storage.head_object(key=staging_key)
        staged_hash = await storage.verify_object_sha256(
            key=staging_key, expected_size_bytes=row.image_size_bytes
        )
        if (
            staged.size_bytes != row.image_size_bytes
            or staged.metadata != metadata
            or staged_hash.size_bytes != row.image_size_bytes
            or staged_hash.sha256 != row.image_sha256
        ):
            raise HTTPException(409, "staged build bytes or metadata do not match")
        await storage.copy_object(source_key=staging_key, dest_key=final_key)
        final = await storage.head_object(key=final_key)
        final_hash = await storage.verify_object_sha256(
            key=final_key, expected_size_bytes=row.image_size_bytes
        )
        if (
            final.size_bytes != row.image_size_bytes
            or final.metadata != metadata
            or final_hash.size_bytes != row.image_size_bytes
            or final_hash.sha256 != row.image_sha256
        ):
            raise HTTPException(409, "copied build bytes or metadata do not match")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, "replay image verification unavailable") from exc
    row = await _active_claim(session, replay_id, worker)
    if row.process_key_sha256 is not None:
        key = await session.get(ScreenerReplayProcessKey, row.process_key_sha256)
        if key is None or key.status != "active":
            raise HTTPException(403, "replay process key revoked during verification")
    if (
        row.image_upload_id is not None
        or row.image_staging_id is None
        or row.image_sha256 != payload.image_sha256
        or row.image_size_bytes != payload.size_bytes
        or row.image_id != payload.image_id
        or row.artifact_sha256 != payload.artifact_sha256
    ):
        raise HTTPException(409, "replay build binding changed during verification")
    if row.image_verified_at is not None:
        return _state(row)
    row.image_verified_at = datetime.now(UTC)
    row.image_verified_storage_key = final_key
    await session.commit()
    return _state(row)


@screener_router.post(
    "/{replay_id}/receipts", response_model=VerificationReplayReceiptState
)
async def append_replay_receipt(
    replay_id: UUID,
    payload: VerificationReplayReceiptRequest,
    request: Request,
    worker: ScreenerDep,
    session: SessionDep,
) -> VerificationReplayReceiptState:
    node = await _require_enrolled_replay_worker(request, worker, session)
    row = await _active_claim(session, replay_id, worker)
    await _require_lease_process(request, session, node, row, "receipts")
    if (
        payload.artifact_sha256 != row.artifact_sha256
        or payload.policy_version != row.policy_version
        or payload.image_upload_id != row.image_upload_id
        or payload.image_sha256 != row.image_sha256
    ):
        raise HTTPException(409, "receipt evidence binding changed")
    if payload.check_code != "archive_sha" and row.image_verified_at is None:
        raise HTTPException(409, "replay image is not verified")
    existing = await session.scalar(
        select(ScreeningVerificationReplayReceipt).where(
            ScreeningVerificationReplayReceipt.replay_id == replay_id,
            ScreeningVerificationReplayReceipt.check_code == payload.check_code,
        )
    )
    if existing is not None:
        if existing.evidence_sha256 != payload.evidence_sha256:
            raise HTTPException(409, "check already has a different receipt")
        return VerificationReplayReceiptState.model_validate(
            existing, from_attributes=True
        )
    receipt = ScreeningVerificationReplayReceipt(
        receipt_id=uuid4(),
        replay_id=replay_id,
        check_code=payload.check_code,
        evidence_sha256=payload.evidence_sha256,
        worker_hotkey=worker,
        created_at=datetime.now(UTC),
    )
    session.add(receipt)
    await session.commit()
    return VerificationReplayReceiptState.model_validate(receipt, from_attributes=True)


@screener_router.post("/{replay_id}/finish", response_model=VerificationReplayState)
async def finish_replay(
    replay_id: UUID,
    payload: VerificationReplayFinish,
    request: Request,
    worker: ScreenerDep,
    session: SessionDep,
) -> VerificationReplayState:
    node = await _require_enrolled_replay_worker(request, worker, session)
    existing = await session.get(ScreeningVerificationReplay, replay_id)
    if existing is None or existing.worker_hotkey != worker:
        raise HTTPException(403, "replay lease belongs to another worker")
    await _require_lease_process(request, session, node, existing, "finish")
    if (
        existing is not None
        and existing.worker_hotkey == worker
        and existing.status == payload.status
        and existing.failure_code == payload.failure_code
        and existing.artifact_sha256 == payload.artifact_sha256
        and existing.image_sha256 == payload.image_sha256
    ):
        return _state(existing)
    row = await _active_claim(session, replay_id, worker)
    if (
        payload.artifact_sha256 != row.artifact_sha256
        or payload.image_sha256 != row.image_sha256
    ):
        raise HTTPException(409, "replay evidence binding changed")
    if payload.status == "reported" and row.image_verified_at is None:
        raise HTTPException(409, "replay image is not verified")
    if (payload.status == "failed") != (payload.failure_code is not None):
        raise HTTPException(422, "failure code is required only for a failed replay")
    row.status = payload.status
    row.failure_code = payload.failure_code
    row.finished_at = datetime.now(UTC)
    await session.commit()
    return _state(row)
