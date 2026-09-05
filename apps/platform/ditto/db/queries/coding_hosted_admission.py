"""Durable admission of pre-approved hosted Coding assignments, never selection."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import exists, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.agent_status import SCOREABLE_AGENT_STATUSES
from ditto.api_models.coding_canonical import coding_canonical_sha256
from ditto.api_models.coding_hosted import HostedCodingRequest
from ditto.api_server.coding_hosted_verification import (
    SignatureVerifier,
    verify_hosted_request,
)
from ditto.db.models import (
    Agent,
    CodingHostedAssignment,
    CodingPrivateV2Release,
    CodingPrivateV2ReleaseEvent,
)
from ditto.db.queries.validator_auth import consume_validator_nonce


class HostedAdmissionError(ValueError):
    """Safe refusal, with no private assignment contents attached."""


@dataclass(frozen=True)
class HostedAssignmentAuthority:
    evaluation_id: UUID
    attempt_id: UUID
    release_row_id: UUID
    registration_sha256: str
    agent_id: UUID
    validator_hotkey: str
    artifact_sha256: str
    screened_image_sha256: str
    selection_sha256: str
    policy_sha256: str
    execution_profile_sha256: str
    grading_profile_sha256: str
    deadline_unix: int

    def projection(self) -> dict[str, Any]:
        raw = asdict(self)
        for name, value in raw.items():
            if name.endswith("_id"):
                if not isinstance(value, UUID) or value.int == 0:
                    raise HostedAdmissionError(
                        "hosted assignment identifier is invalid"
                    )
                raw[name] = str(value)
            elif name.endswith("_sha256"):
                if not isinstance(value, str) or not re.fullmatch(
                    r"[0-9a-f]{64}", value
                ):
                    raise HostedAdmissionError("hosted assignment digest is invalid")
        if (
            not isinstance(self.validator_hotkey, str)
            or not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{47,48}", self.validator_hotkey)
            or type(self.deadline_unix) is not int
            or not 0 < self.deadline_unix <= 253402300799
        ):
            raise HostedAdmissionError("hosted assignment bounds are invalid")
        return {
            "schema": "dittobench-coding-hosted-assignment-v2",
            "coding_contract_version": 2,
            "shadow_only": True,
            "weight_eligible": False,
            **raw,
        }

    def digest(self) -> str:
        return coding_canonical_sha256(
            self.projection(), maximum_bytes=16384, label="hosted assignment"
        )


@dataclass(frozen=True)
class HostedAdmissionView:
    evaluation_id: UUID
    attempt_id: UUID
    state: Literal["assigned", "admitted", "started"]
    newly_admitted: bool = False
    newly_started: bool = False


async def create_hosted_assignment(
    session: AsyncSession,
    *,
    authority: HostedAssignmentAuthority,
    confirmed_assignment_sha256: str,
    actor: str,
    reason: str,
) -> CodingHostedAssignment:
    """Trusted operator path only; validator requests cannot create assignments.

    Call inside the caller's transaction. Explicit approval binds the exact
    authority digest; a registered release alone is not an execution approval.
    No HTTP route invokes this function by default.
    """
    projection = authority.projection()
    digest = authority.digest()
    if (
        confirmed_assignment_sha256 != digest
        or not 1 <= len(actor.strip()) <= 120
        or not 8 <= len(reason.strip()) <= 512
    ):
        raise HostedAdmissionError("hosted assignment approval is invalid")
    await _lock_authorities(
        session,
        authority.release_row_id,
        authority.registration_sha256,
        authority.agent_id,
        authority.artifact_sha256,
        authority.screened_image_sha256,
    )
    now = await _now(session)
    deadline = datetime.fromtimestamp(authority.deadline_unix, UTC)
    if not now < deadline or (deadline - now).total_seconds() > 3600:
        raise HostedAdmissionError("hosted assignment deadline is invalid")
    await session.execute(
        pg_insert(CodingHostedAssignment)
        .values(
            evaluation_id=authority.evaluation_id,
            attempt_id=authority.attempt_id,
            release_row_id=authority.release_row_id,
            registration_sha256=authority.registration_sha256,
            agent_id=authority.agent_id,
            validator_hotkey=authority.validator_hotkey,
            artifact_sha256=authority.artifact_sha256,
            screened_image_sha256=authority.screened_image_sha256,
            assignment_sha256=digest,
            authority=projection,
            expires_at=deadline,
            reason=reason.strip(),
            actor=actor.strip(),
            shadow_only=True,
            weight_eligible=False,
        )
        .on_conflict_do_nothing()
    )
    row = await session.get(
        CodingHostedAssignment, authority.evaluation_id, populate_existing=True
    )
    if row is None or row.assignment_sha256 != digest or row.authority != projection:
        raise HostedAdmissionError(
            "hosted assignment conflicts with existing authority"
        )
    return row


async def admit_hosted_request(
    session: AsyncSession,
    *,
    request: HostedCodingRequest,
    authenticated_validator: str,
    verifier: SignatureVerifier,
) -> HostedAdmissionView:
    """Consume a signed nonce and admit only an existing immutable assignment."""
    now = await _now(session)
    request_sha = verify_hosted_request(
        request=request,
        expected_validator=authenticated_validator,
        verifier=verifier,
        now_unix=int(now.timestamp()),
    )
    # Revalidate the object used below, not just the verifier's canonical copy.
    request = HostedCodingRequest.model_validate(
        request.model_dump(mode="json", by_alias=True)
    )
    row = await _locked_assignment(session, request.evaluation_id)
    if (
        row.validator_hotkey != authenticated_validator
        or row.artifact_sha256 != request.artifact_sha256
        or row.assignment_sha256 != request.assignment_sha256
        or row.authority["policy_sha256"] != request.policy_sha256
    ):
        raise HostedAdmissionError("hosted request assignment does not match")
    now = await _now(session)
    if not request.issued_at_unix <= now.timestamp() < request.expires_at_unix:
        raise HostedAdmissionError("hosted request expired while awaiting admission")
    if request.operation == "acknowledge":
        raise HostedAdmissionError("hosted terminal acknowledgement is not available")
    if request.operation == "evaluate" and row.expires_at <= now:
        raise HostedAdmissionError("hosted assignment expired")
    await consume_validator_nonce(
        session,
        nonce=request.nonce,
        validator_hotkey=authenticated_validator,
        now=now,
        expires_at=datetime.fromtimestamp(request.expires_at_unix, UTC),
    )
    newly_admitted = request.operation == "evaluate" and row.admitted_at is None
    if newly_admitted:
        row.admitted_at = now
        row.admission_request_sha256 = request_sha
        await session.flush()
    return _view(row, newly_admitted=newly_admitted)


async def start_hosted_attempt(
    session: AsyncSession,
    *,
    evaluation_id: UUID,
    expected_attempt_id: UUID,
    worker_id: UUID,
) -> HostedAdmissionView:
    """Commit before launching candidate code; a replay never authorizes a rerun."""
    if not isinstance(worker_id, UUID) or worker_id.int == 0:
        raise HostedAdmissionError("hosted worker identity is invalid")
    row = await _locked_assignment(session, evaluation_id)
    if row.attempt_id != expected_attempt_id or row.admitted_at is None:
        raise HostedAdmissionError("hosted attempt is not admitted")
    if row.started_at is not None:
        if row.worker_id != worker_id:
            raise HostedAdmissionError(
                "hosted attempt already belongs to another worker"
            )
        return _view(row)
    now = await _now(session)
    if row.expires_at <= now:
        raise HostedAdmissionError("hosted assignment expired")
    row.started_at, row.worker_id = now, worker_id
    await session.flush()
    return _view(row, newly_started=True)


async def _locked_assignment(
    session: AsyncSession, evaluation_id: UUID
) -> CodingHostedAssignment:
    snapshot = await session.get(
        CodingHostedAssignment, evaluation_id, populate_existing=True
    )
    if snapshot is None:
        raise HostedAdmissionError("hosted assignment is unavailable")
    # Global order: release -> agent -> assignment. Retirement takes the same
    # release lock, so it cannot interleave with a successful admission/start.
    await _lock_authorities(
        session,
        snapshot.release_row_id,
        snapshot.registration_sha256,
        snapshot.agent_id,
        snapshot.artifact_sha256,
        snapshot.screened_image_sha256,
    )
    row = await session.scalar(
        select(CodingHostedAssignment)
        .where(CodingHostedAssignment.evaluation_id == evaluation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise HostedAdmissionError("hosted assignment is unavailable")
    return row


async def _lock_authorities(
    session: AsyncSession,
    release_id: UUID,
    registration_sha: str,
    agent_id: UUID,
    artifact_sha: str,
    image_sha: str,
) -> None:
    release = await session.get(
        CodingPrivateV2Release, release_id, with_for_update=True, populate_existing=True
    )
    inactive = await session.scalar(
        select(exists().where(CodingPrivateV2ReleaseEvent.release_row_id == release_id))
    )
    if (
        release is None
        or release.registration_sha256 != registration_sha
        or inactive
        or release.shadow_only is not True
        or release.weight_eligible is not False
    ):
        raise HostedAdmissionError("hosted release is unavailable")
    agent = await session.get(
        Agent, agent_id, with_for_update=True, populate_existing=True
    )
    if (
        agent is None
        or agent.sha256 != artifact_sha
        or agent.screened_image_sha256 != image_sha
        or agent.status not in SCOREABLE_AGENT_STATUSES
    ):
        raise HostedAdmissionError("hosted screened artifact is unavailable")


async def _now(session: AsyncSession) -> datetime:
    value = await session.scalar(select(func.clock_timestamp()))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise HostedAdmissionError("hosted database clock is unavailable")
    return value


def _view(
    row: CodingHostedAssignment,
    *,
    newly_admitted: bool = False,
    newly_started: bool = False,
) -> HostedAdmissionView:
    state: Literal["assigned", "admitted", "started"] = "assigned"
    if row.admitted_at is not None:
        state = "admitted"
    if row.started_at is not None:
        state = "started"
    return HostedAdmissionView(
        row.evaluation_id, row.attempt_id, state, newly_admitted, newly_started
    )
