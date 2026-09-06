"""PostgreSQL-backed one-time start handoff for the Platform-owned worker."""

from __future__ import annotations

import asyncio
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.coding_hosted_start import HostedStartRequest
from ditto.db.models import Agent
from ditto.db.queries.coding_hosted_admission import (
    HostedAdmissionError,
    HostedAssignmentAuthority,
    _locked_assignment,
    _now,
    start_hosted_attempt,
)
from ditto.db.queries.coding_hosted_private import _selection_matches, _task
from ditto_screening_protocol import SCREENING_POLICY_VERSION


class HostedStartStore:
    """Worker identity is fixed by trusted process configuration, not stdin."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession], worker_id: UUID):
        if not isinstance(worker_id, UUID) or not worker_id.int:
            raise HostedAdmissionError("hosted start worker is invalid")
        self._sessions, self._worker_id = sessions, worker_id

    async def commit_start(self, request: HostedStartRequest) -> bool:
        request = HostedStartRequest.model_validate(
            request.model_dump(mode="json", by_alias=True)
        )
        if request.worker_id != self._worker_id:
            raise HostedAdmissionError("hosted start worker does not match")
        async with asyncio.timeout(20):
            async with self._sessions() as session, session.begin():
                row = await _locked_assignment(session, request.evaluation_id)
                # Reconstruct the known assignment rather than trusting a digest
                # column or a partial worker-supplied projection alone.
                names = HostedAssignmentAuthority.__dataclass_fields__
                values = {name: row.authority[name] for name in names}
                for name in names:
                    if name.endswith("_id"):
                        values[name] = UUID(values[name])
                authority = HostedAssignmentAuthority(**values)
                if (
                    authority.projection() != row.authority
                    or authority.digest() != row.assignment_sha256
                    or row.assignment_sha256 != request.assignment_sha256
                    or row.attempt_id != request.attempt_id
                    or row.agent_id != request.agent_id
                    or row.artifact_sha256 != request.artifact_sha256
                    or row.screened_image_sha256 != request.screened_image_sha256
                    or row.expires_at.timestamp() != request.deadline_unix
                    or row.admitted_at is None
                ):
                    raise HostedAdmissionError("hosted start assignment does not match")
                agent = await session.get(Agent, row.agent_id)
                if (
                    agent is None
                    or agent.screened_image_size_bytes
                    != request.screened_image_size_bytes
                    or agent.screened_image_id != request.screened_image_id
                    or agent.screened_image_ref != request.screened_image_ref
                    or agent.screening_policy_version
                    != request.screening_policy_version
                    or agent.screening_policy_version < SCREENING_POLICY_VERSION
                    or agent.screened_image_verified_at is None
                ):
                    raise HostedAdmissionError(
                        "hosted start screened image does not match"
                    )
                task = await _task(session, row.evaluation_id)
                if (
                    task is None
                    or not _selection_matches(task, row)
                    or task.closed_at is not None
                    or task.frozen_at is not None
                    or row.expires_at <= await _now(session)
                ):
                    raise HostedAdmissionError(
                        "hosted start private task is unavailable"
                    )
                result = await start_hosted_attempt(
                    session,
                    evaluation_id=request.evaluation_id,
                    expected_attempt_id=request.attempt_id,
                    worker_id=self._worker_id,
                )
            # The transaction has committed. No successful handoff may escape
            # inside session.begin(), including on cancellation/commit failure.
            return result.newly_started
