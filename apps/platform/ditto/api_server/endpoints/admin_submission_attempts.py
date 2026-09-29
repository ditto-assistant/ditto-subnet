"""Explicit operator reads of past paid archives; no live upload profiling."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.submission_attempts import (
    AttemptComparison,
    AttemptObservationPolicy,
)
from ditto.api_server.attestation import expected_netuid
from ditto.api_server.dependencies import get_session, get_storage_client
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.api_server.storage import ObjectDownloadFailedError, S3StorageClient
from ditto.api_server.submission_attempts import (
    MAX_ARCHIVE_BYTES,
    ProfileUnavailable,
    artifact_profile,
    classify_pair,
    observation_policy,
)
from ditto.db.queries.submission_attempts import (
    PaidAgent,
    latest_paid_predecessor,
    owner_scope,
    paid_agent,
    reference_feedback,
)

router = APIRouter(prefix="/admin/submission-attempts", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
StorageDep = Annotated[S3StorageClient, Depends(get_storage_client)]
AdminDep = Annotated[None, Depends(require_admin)]


def _policy(request: Request) -> AttemptObservationPolicy:
    return observation_policy(request.app.state.config.commit_hash)


@router.get("", response_model=AttemptObservationPolicy)
async def get_policy(
    request: Request, response: Response, _admin: AdminDep
) -> AttemptObservationPolicy:
    response.headers["Cache-Control"] = "no-store"
    return _policy(request)


async def _profile(agent: PaidAgent, storage: S3StorageClient):
    if agent.size_bytes is None or not 0 < agent.size_bytes <= MAX_ARCHIVE_BYTES:
        raise ProfileUnavailable(
            "Recorded archive size is unavailable or exceeds the observation budget."
        )
    data = await storage.get_object(
        key=f"{agent.agent_id}/agent.tar.gz", max_bytes=MAX_ARCHIVE_BYTES
    )

    def verify():
        if (
            len(data) != agent.size_bytes
            or hashlib.sha256(data).hexdigest() != agent.sha256
        ):
            raise ProfileUnavailable(
                "Stored archive does not match the paid submission's digest and size."
            )
        return artifact_profile(data)

    return await asyncio.to_thread(verify)


@router.get("/{agent_id}", response_model=AttemptComparison)
async def compare_paid_submission(
    agent_id: UUID,
    request: Request,
    response: Response,
    _admin: AdminDep,
    session: SessionDep,
    storage: StorageDep,
    reference_agent_id: UUID | None = None,
) -> AttemptComparison:
    response.headers["Cache-Control"] = "no-store"
    identity = await paid_agent(session, agent_id)
    if identity is None:
        raise HTTPException(404, "paid submission not found")
    candidate = identity
    as_of = candidate.created_at
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=UTC)
    values: dict[str, Any] = {
        "policy": _policy(request),
        "agent_id": agent_id,
        "as_of": as_of,
        "sha256": candidate.sha256,
        "reference_agent_id": reference_agent_id,
    }
    try:
        owners = await owner_scope(
            session, coldkey=candidate.coldkey, netuid=expected_netuid(), as_of=as_of
        )
        reference_id = reference_agent_id or await latest_paid_predecessor(
            session, owners=owners, as_of=as_of, candidate_id=agent_id
        )
        values["reference_agent_id"] = reference_id
        if reference_id is None:
            await session.rollback()
            await _profile(candidate, storage)
            return AttemptComparison(
                **values,
                classification="first_submission",
                reason="No earlier paid submission in the proven payer scope.",
            )
        reference_identity = await paid_agent(session, reference_id)
        if reference_identity is None:
            raise ProfileUnavailable("Reference has no paid submission identity.")
        reference = reference_identity
        reference_at = reference.created_at
        if reference_at.tzinfo is None:
            reference_at = reference_at.replace(tzinfo=UTC)
        if reference.coldkey not in owners or reference_at >= as_of:
            raise ProfileUnavailable(
                "Reference must be an earlier submission in the proven payer scope."
            )
        values["reference_sha256"] = reference.sha256
        status, reason, finished_at = await reference_feedback(
            session, agent_id=reference_id, as_of=as_of
        )
        values.update(
            feedback_status=status, feedback_reason=reason, feedback_at=finished_at
        )
        # Each object read is capped. Do not retain a DB connection during S3
        # or CPU work. These reads intentionally take no mutation/admission lock.
        await session.rollback()
        current_profile = await _profile(candidate, storage)
        previous_profile = await _profile(reference, storage)
        kind, detail = await asyncio.to_thread(
            classify_pair,
            current_profile,
            previous_profile,
            infrastructure_failure=status == "infrastructure",
        )
        return AttemptComparison(**values, classification=kind, reason=detail)
    except (ProfileUnavailable, ObjectDownloadFailedError) as error:
        detail = (
            str(error)
            if isinstance(error, ProfileUnavailable)
            else "Stored artifact is unavailable."
        )
        return AttemptComparison(**values, classification="inconclusive", reason=detail)
