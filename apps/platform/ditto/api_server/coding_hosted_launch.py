"""Read-only pre-start assignment projection, exclusively inside Platform."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.coding_private_v2_registry import (
    CodingPrivateV2RegistrationAuthority,
)
from ditto.api_server.coding_private_v2_retrieval import PrivateV2InputRetriever
from ditto.api_server.storage import S3StorageClient
from ditto.db.models import Agent, CodingPrivateV2Release
from ditto.db.queries.coding_hosted_admission import (
    HostedAssignmentAuthority,
    _locked_assignment,
    _now,
)
from ditto.db.queries.coding_hosted_private import _selection_matches, _task
from ditto_screening_protocol import SCREENING_POLICY_VERSION


class HostedLaunchError(ValueError):
    """Redacted refusal; never grants a candidate start."""


@dataclass(frozen=True, repr=False)
class HostedLaunchProjection:
    authority: HostedAssignmentAuthority
    registration: CodingPrivateV2RegistrationAuthority
    catalog_index: int
    max_patch_bytes: int
    image_upload_id: UUID
    image_id: str
    image_ref: str
    image_size: int
    screening_policy_version: int


async def inspect_launch(
    sessions: async_sessionmaker[AsyncSession],
    *,
    evaluation_id: UUID,
    attempt_id: UUID,
    assignment_sha256: str,
    policy_sha256: str,
    execution_profile_sha256: str,
    grading_profile_sha256: str,
) -> HostedLaunchProjection:
    async with asyncio.timeout(20), sessions() as session, session.begin():
        row = await _locked_assignment(session, evaluation_id)
        names = HostedAssignmentAuthority.__dataclass_fields__
        values = {name: row.authority[name] for name in names}
        for name in names:
            if name.endswith("_id"):
                values[name] = UUID(values[name])
        authority = HostedAssignmentAuthority(**values)
        if (
            authority.projection() != row.authority
            or authority.digest() != row.assignment_sha256
            or row.assignment_sha256 != assignment_sha256
            or row.attempt_id != attempt_id
            or row.admitted_at is None
            or row.started_at is not None
            or row.worker_id is not None
            or row.expires_at.timestamp() != authority.deadline_unix
            or not 0 < (row.expires_at - await _now(session)).total_seconds() <= 3600
            or authority.policy_sha256 != policy_sha256
            or authority.execution_profile_sha256 != execution_profile_sha256
            or authority.grading_profile_sha256 != grading_profile_sha256
        ):
            raise HostedLaunchError("hosted launch authority unavailable")
        task = await _task(session, evaluation_id)
        agent = await session.get(Agent, row.agent_id)
        release = await session.get(CodingPrivateV2Release, row.release_row_id)
        if (
            task is None
            or task.closed_at is not None
            or task.frozen_at is not None
            or not _selection_matches(task, row)
            or agent is None
            or release is None
            or agent.screened_image_verified_at is None
            or agent.screened_image_upload_id is None
            or agent.screened_image_size_bytes is None
            or agent.screened_image_id is None
            or agent.screened_image_ref is None
            or agent.screening_policy_version is None
            or agent.screening_policy_version < SCREENING_POLICY_VERSION
            or authority.agent_id != row.agent_id
            or authority.artifact_sha256 != row.artifact_sha256
            or authority.screened_image_sha256 != row.screened_image_sha256
            or authority.registration_sha256 != row.registration_sha256
            or authority.release_row_id != row.release_row_id
        ):
            raise HostedLaunchError("hosted launch inputs unavailable")
        registration = CodingPrivateV2RegistrationAuthority.model_validate(
            release.registration_authority
        )
        if registration.registration_sha256 != authority.registration_sha256:
            raise HostedLaunchError("hosted launch registration differs")
        return HostedLaunchProjection(
            authority,
            registration,
            task.catalog_index,
            task.max_patch_bytes,
            agent.screened_image_upload_id,
            agent.screened_image_id,
            agent.screened_image_ref,
            agent.screened_image_size_bytes,
            agent.screening_policy_version,
        )


async def prepare_launch(
    sessions: async_sessionmaker[AsyncSession],
    *,
    projection: HostedLaunchProjection,
    worker_id: UUID,
    retriever: PrivateV2InputRetriever,
    storage: S3StorageClient,
) -> tuple[dict, dict]:
    from ditto.api_server.endpoints.validator import _screened_image_key

    authority = projection.authority
    if not isinstance(worker_id, UUID) or not worker_id.int:
        raise HostedLaunchError("hosted launch worker invalid")
    metadata = retriever.describe_selection(projection.catalog_index)
    if metadata.registration_sha256 != authority.registration_sha256:
        raise HostedLaunchError("hosted launch payload differs")
    issued = datetime.now(UTC)
    ttl = min(300, int(authority.deadline_unix - issued.timestamp()))
    if ttl < 1:
        raise HostedLaunchError("hosted launch expired")
    async with asyncio.timeout(20):
        url = await storage.presigned_get_url(
            key=_screened_image_key(authority.agent_id, projection.image_upload_id),
            expires_in=ttl,
        )
    refreshed = await inspect_launch(
        sessions,
        evaluation_id=authority.evaluation_id,
        attempt_id=authority.attempt_id,
        assignment_sha256=authority.digest(),
        policy_sha256=authority.policy_sha256,
        execution_profile_sha256=authority.execution_profile_sha256,
        grading_profile_sha256=authority.grading_profile_sha256,
    )
    if refreshed != projection:
        raise HostedLaunchError("hosted launch changed during image handoff")
    expected = {
        "evaluation_id": str(authority.evaluation_id),
        "attempt_id": str(authority.attempt_id),
        "worker_id": str(worker_id),
        "assignment_sha256": authority.digest(),
        "registration_sha256": authority.registration_sha256,
        "execution_profile_sha256": authority.execution_profile_sha256,
        "task_commitment_sha256": metadata.task_commitment_sha256,
        "deadline_unix": authority.deadline_unix,
        "max_patch_bytes": projection.max_patch_bytes,
        "catalog_index": projection.catalog_index,
        "corpus_release_id": metadata.corpus_release_id,
        "private_release_sha256": metadata.private_release_sha256,
    }
    harness = {
        "EvaluationID": str(authority.evaluation_id),
        "AttemptID": str(authority.attempt_id),
        "WorkerID": str(worker_id),
        "AssignmentSHA256": authority.digest(),
        "AgentID": str(authority.agent_id),
        "AgentArtifactSHA256": authority.artifact_sha256,
        "ProfileCapabilityID": f"hosted-{authority.attempt_id}",
        "Deadline": datetime.fromtimestamp(authority.deadline_unix, UTC).isoformat(),
        "ScreenedImageSHA256": authority.screened_image_sha256,
        "ScreenedImageID": projection.image_id,
        "ScreenedImageRef": projection.image_ref,
        "ScreenedImageSize": projection.image_size,
        "ScreeningPolicyVersion": projection.screening_policy_version,
        "ImageURL": url,
        "ImageExpiresAt": (issued + timedelta(seconds=ttl)).isoformat(),
    }
    return expected, harness
