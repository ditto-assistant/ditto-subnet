"""Durable Platform-private task grants; never called from validator routes."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.coding_hosted_private import (
    AUTHORING_ROLES,
    GRADING_ROLES,
    HostedTaskSelection,
    PrivateV2ObjectGrant,
)
from ditto.db.models import CodingHostedAssignment, CodingHostedPrivateTask
from ditto.db.queries.coding_hosted_admission import (
    HostedAdmissionError,
    _locked_assignment,
    _now,
)


class HostedPrivateTaskError(ValueError):
    """Safe refusal with no task data or provider details."""


@dataclass(frozen=True, repr=False)
class HostedTaskGrants:
    authoring_grant_id: UUID
    grading_grant_id: UUID


@dataclass(frozen=True, repr=False)
class HostedPatchFreeze:
    patch_sha256: str
    patch_size_bytes: int
    newly_frozen: bool


async def bind_hosted_private_task(
    session: AsyncSession,
    *,
    selection: HostedTaskSelection,
) -> HostedTaskGrants:
    """Bind the pre-approved selection before start, inside the caller's transaction.

    Assignment approval must commit to this exact selection digest. This is not
    a selector, canary qualification or an alternative approval mechanism.
    """
    projection = selection.projection()
    assignment = await _locked_assignment(session, selection.evaluation_id)
    if (
        assignment.attempt_id != selection.attempt_id
        or assignment.registration_sha256 != selection.registration_sha256
        or assignment.artifact_sha256 != selection.artifact_sha256
        or assignment.authority["selection_sha256"] != selection.digest()
    ):
        raise HostedPrivateTaskError("hosted private selection does not match")
    task = await _task(session, selection.evaluation_id)
    if task is not None:
        if (
            task.selection_authority != projection
            or task.selection_sha256 != selection.digest()
        ):
            raise HostedPrivateTaskError("hosted private selection conflicts")
        return HostedTaskGrants(task.authoring_grant_id, task.grading_grant_id)
    now = await _now(session)
    if assignment.started_at is not None or now >= assignment.expires_at:
        raise HostedPrivateTaskError("hosted private selection binding is closed")
    await session.execute(
        pg_insert(CodingHostedPrivateTask).values(
            evaluation_id=selection.evaluation_id,
            selection_sha256=selection.digest(),
            selection_authority=projection,
            catalog_index=selection.catalog_index,
            max_patch_bytes=selection.max_patch_bytes,
            authoring_grant_id=uuid4(),
            grading_grant_id=uuid4(),
        )
    )
    task = await _task(session, selection.evaluation_id)
    assert task is not None
    return HostedTaskGrants(task.authoring_grant_id, task.grading_grant_id)


async def active_hosted_object_grant(
    session: AsyncSession,
    *,
    grant_id: UUID,
    worker_id: UUID,
    audience: Literal["platform-authoring", "platform-grading"],
) -> PrivateV2ObjectGrant | None:
    """Recheck committed ownership/lifecycle/phase on every retrieval boundary."""
    if (
        not isinstance(grant_id, UUID)
        or grant_id.int == 0
        or not isinstance(worker_id, UUID)
        or worker_id.int == 0
        or audience not in {"platform-authoring", "platform-grading"}
    ):
        return None
    column = (
        CodingHostedPrivateTask.authoring_grant_id
        if audience == "platform-authoring"
        else CodingHostedPrivateTask.grading_grant_id
    )
    evaluation_id = await session.scalar(
        select(CodingHostedPrivateTask.evaluation_id).where(column == grant_id)
    )
    if evaluation_id is None:
        return None
    try:
        assignment = await _locked_assignment(session, evaluation_id)
    except HostedAdmissionError:
        return None
    task = await _task(session, evaluation_id)
    now = await _now(session)
    if (
        task is None
        or task.closed_at is not None
        or assignment.worker_id != worker_id
        or assignment.started_at is None
        or now >= assignment.expires_at
        or task.selection_sha256 != assignment.authority["selection_sha256"]
    ):
        return None
    if not _selection_matches(task, assignment):
        return None
    authoring = audience == "platform-authoring"
    if authoring == (task.frozen_at is not None):
        return None
    return PrivateV2ObjectGrant(
        grant_id=grant_id,
        evaluation_id=evaluation_id,
        attempt_id=assignment.attempt_id,
        registration_sha256=assignment.registration_sha256,
        catalog_index=task.catalog_index,
        phase="authoring" if authoring else "grading",
        audience=audience,
        allowed_roles=AUTHORING_ROLES if authoring else GRADING_ROLES,
        expires_at_unix=int(assignment.expires_at.timestamp()),
        frozen_patch_sha256=task.frozen_patch_sha256,
    )


async def freeze_hosted_private_patch(
    session: AsyncSession,
    *,
    evaluation_id: UUID,
    attempt_id: UUID,
    worker_id: UUID,
    patch: bytes,
) -> HostedPatchFreeze:
    """Commit one observed patch identity and revoke authoring object grants.

    The trusted supervisor must first quiesce candidate execution and revoke its
    relay, and must preserve these exact bytes for pristine grading and sealing.
    This ledger does not claim process termination or store plaintext patch bytes.
    """
    assignment = await _locked_assignment(session, evaluation_id)
    _require_worker(assignment, attempt_id, worker_id)
    task = await _task(session, evaluation_id)
    if task is None or type(patch) is not bytes or len(patch) > task.max_patch_bytes:
        raise HostedPrivateTaskError("hosted frozen patch is invalid")
    if not _selection_matches(task, assignment):
        raise HostedPrivateTaskError("hosted private selection is invalid")
    digest = hashlib.sha256(patch).hexdigest()
    if task.frozen_at is not None:
        if task.frozen_patch_sha256 != digest or task.frozen_patch_size != len(patch):
            raise HostedPrivateTaskError("hosted frozen patch conflicts")
        return HostedPatchFreeze(digest, len(patch), False)
    now = await _now(session)
    if task.closed_at is not None or now >= assignment.expires_at:
        raise HostedPrivateTaskError("hosted patch freeze is closed")
    task.frozen_at, task.frozen_patch_sha256, task.frozen_patch_size = (
        now,
        digest,
        len(patch),
    )
    await session.flush()
    return HostedPatchFreeze(digest, len(patch), True)


async def close_hosted_private_task(
    session: AsyncSession,
    *,
    evaluation_id: UUID,
    attempt_id: UUID,
    worker_id: UUID,
    reason: Literal["completed", "failed", "aborted"],
) -> bool:
    """Revoke both object phases even after expiry, retirement or artifact drift.

    Closing is not terminal scoring/evidence finalization. It only removes access.
    """
    if reason not in {"completed", "failed", "aborted"}:
        raise HostedPrivateTaskError("hosted close reason is invalid")
    # Access removal needs only assignment -> task locks; never acquire release
    # after assignment. No new authority or plaintext is returned by this path.
    assignment = await session.get(
        CodingHostedAssignment,
        evaluation_id,
        with_for_update=True,
        populate_existing=True,
    )
    if assignment is None:
        raise HostedPrivateTaskError("hosted assignment is unavailable")
    _require_worker(assignment, attempt_id, worker_id)
    task = await _task(session, evaluation_id)
    if task is None:
        raise HostedPrivateTaskError("hosted private task is unavailable")
    if task.closed_at is not None:
        return False
    task.closed_at, task.close_reason = await _now(session), reason
    await session.flush()
    return True


def _selection_matches(
    task: CodingHostedPrivateTask, assignment: CodingHostedAssignment
) -> bool:
    try:
        value = task.selection_authority
        selection = HostedTaskSelection(
            evaluation_id=UUID(value["evaluation_id"]),
            attempt_id=UUID(value["attempt_id"]),
            registration_sha256=value["registration_sha256"],
            artifact_sha256=value["artifact_sha256"],
            schedule_sha256=value["schedule_sha256"],
            catalog_index=value["catalog_index"],
            max_patch_bytes=value["max_patch_bytes"],
        )
        return (
            selection.projection() == value
            and selection.digest()
            == task.selection_sha256
            == assignment.authority["selection_sha256"]
            and selection.evaluation_id == assignment.evaluation_id
            and selection.attempt_id == assignment.attempt_id
            and selection.registration_sha256 == assignment.registration_sha256
            and selection.artifact_sha256 == assignment.artifact_sha256
            and selection.catalog_index == task.catalog_index
            and selection.max_patch_bytes == task.max_patch_bytes
        )
    except (AttributeError, KeyError, TypeError, ValueError):
        return False


def _require_worker(
    assignment: CodingHostedAssignment, attempt_id: UUID, worker_id: UUID
) -> None:
    if (
        not isinstance(worker_id, UUID)
        or worker_id.int == 0
        or not isinstance(attempt_id, UUID)
        or attempt_id.int == 0
        or assignment.attempt_id != attempt_id
        or assignment.worker_id != worker_id
        or assignment.started_at is None
    ):
        raise HostedPrivateTaskError("hosted task worker does not match")


async def _task(
    session: AsyncSession, evaluation_id: UUID
) -> CodingHostedPrivateTask | None:
    return await session.scalar(
        select(CodingHostedPrivateTask)
        .where(CodingHostedPrivateTask.evaluation_id == evaluation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
