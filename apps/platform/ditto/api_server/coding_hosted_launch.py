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
from ditto.db.models import (
    Agent,
    CodingHostedAssignment,
    CodingHostedPrivateTask,
    CodingPrivateV2Release,
)
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


# An assignment launches only inside its final hour.
LAUNCH_WINDOW_SECONDS = 3600


def stored_authority(row: CodingHostedAssignment) -> HostedAssignmentAuthority:
    """Rebuild the stored assignment authority; a malformed projection raises."""
    names = HostedAssignmentAuthority.__dataclass_fields__
    values = {name: row.authority[name] for name in names}
    for name in names:
        if name.endswith("_id"):
            values[name] = UUID(values[name])
    return HostedAssignmentAuthority(**values)


# The predicates below are lock-agnostic and shared with the fixed-host attempt
# config materializer, which reads the same rows in a read-only snapshot.
def authority_bound(
    row: CodingHostedAssignment,
    authority: HostedAssignmentAuthority,
    *,
    assignment_sha256: str,
    attempt_id: UUID,
    policy_sha256: str,
    execution_profile_sha256: str,
    grading_profile_sha256: str,
) -> bool:
    """Projection, digests, pins, deadline and every denormalized column agree."""
    return (
        authority.projection() == row.authority
        and authority.digest() == row.assignment_sha256 == assignment_sha256
        and (
            authority.evaluation_id,
            authority.attempt_id,
            authority.release_row_id,
            authority.registration_sha256,
            authority.agent_id,
            authority.validator_hotkey,
            authority.artifact_sha256,
            authority.screened_image_sha256,
        )
        == (
            row.evaluation_id,
            row.attempt_id,
            row.release_row_id,
            row.registration_sha256,
            row.agent_id,
            row.validator_hotkey,
            row.artifact_sha256,
            row.screened_image_sha256,
        )
        and row.attempt_id == attempt_id
        and row.expires_at.timestamp() == authority.deadline_unix
        and row.shadow_only is True
        and row.weight_eligible is False
        and authority.policy_sha256 == policy_sha256
        and authority.execution_profile_sha256 == execution_profile_sha256
        and authority.grading_profile_sha256 == grading_profile_sha256
    )


def launch_pending(
    row: CodingHostedAssignment, now: datetime, *, minimum_remaining: float = 0
) -> bool:
    """Admitted, unstarted and unowned, with the deadline inside the launch window."""
    remaining = (row.expires_at - now).total_seconds()
    return (
        row.admitted_at is not None
        and row.started_at is None
        and row.worker_id is None
        and 0 < remaining <= LAUNCH_WINDOW_SECONDS
        and remaining >= minimum_remaining
    )


def screened_image_ready(agent: Agent | None) -> bool:
    return (
        agent is not None
        and agent.screened_image_verified_at is not None
        and agent.screened_image_upload_id is not None
        and agent.screened_image_size_bytes is not None
        and agent.screened_image_id is not None
        and agent.screened_image_ref is not None
        and agent.screening_policy_version is not None
        and agent.screening_policy_version >= SCREENING_POLICY_VERSION
    )


def task_open(
    task: CodingHostedPrivateTask | None, row: CodingHostedAssignment
) -> bool:
    return (
        task is not None
        and task.closed_at is None
        and task.frozen_at is None
        and _selection_matches(task, row)
    )


def launch_registration(
    release: CodingPrivateV2Release, authority: HostedAssignmentAuthority
) -> CodingPrivateV2RegistrationAuthority | None:
    registration = CodingPrivateV2RegistrationAuthority.model_validate(
        release.registration_authority
    )
    if registration.registration_sha256 != authority.registration_sha256:
        return None
    return registration


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
        authority = stored_authority(row)
        if not authority_bound(
            row,
            authority,
            assignment_sha256=assignment_sha256,
            attempt_id=attempt_id,
            policy_sha256=policy_sha256,
            execution_profile_sha256=execution_profile_sha256,
            grading_profile_sha256=grading_profile_sha256,
        ) or not launch_pending(row, await _now(session)):
            raise HostedLaunchError("hosted launch authority unavailable")
        task = await _task(session, evaluation_id)
        agent = await session.get(Agent, row.agent_id)
        release = await session.get(CodingPrivateV2Release, row.release_row_id)
        if (
            release is None
            or not task_open(task, row)
            or not screened_image_ready(agent)
        ):
            raise HostedLaunchError("hosted launch inputs unavailable")
        registration = launch_registration(release, authority)
        if registration is None:
            raise HostedLaunchError("hosted launch registration differs")
        # Narrowing only; task_open and screened_image_ready already required these.
        assert (
            task is not None
            and agent is not None
            and agent.screened_image_upload_id is not None
            and agent.screened_image_id is not None
            and agent.screened_image_ref is not None
            and agent.screened_image_size_bytes is not None
            and agent.screening_policy_version is not None
        )
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
