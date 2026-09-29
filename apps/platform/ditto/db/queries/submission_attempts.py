"""Read paid identities and feedback without locks, reservations, or writes."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import case, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.ticket_status import TicketPurpose
from ditto.api_server.submission_attempts import MAX_OWNER_LINKS, ProfileUnavailable
from ditto.db.models import (
    Agent,
    EvaluationPayment,
    OwnerAttestation,
    ScreeningAttempt,
    ValidatorTicket,
)


@dataclass(frozen=True)
class PaidAgent:
    agent_id: UUID
    coldkey: str
    created_at: datetime
    sha256: str
    size_bytes: int | None


async def paid_agent(session: AsyncSession, agent_id: UUID) -> PaidAgent | None:
    row = (
        await session.execute(
            select(
                Agent.agent_id,
                EvaluationPayment.miner_coldkey,
                Agent.created_at,
                Agent.sha256,
                Agent.size_bytes,
            )
            .join(EvaluationPayment, EvaluationPayment.agent_id == Agent.agent_id)
            .where(Agent.agent_id == agent_id)
        )
    ).one_or_none()
    return PaidAgent(*row) if row is not None else None


async def owner_scope(
    session: AsyncSession, *, coldkey: str, netuid: int, as_of: datetime
) -> set[str]:
    peers = list(
        await session.scalars(
            select(
                case(
                    (OwnerAttestation.lo_signer == coldkey, OwnerAttestation.hi_signer),
                    else_=OwnerAttestation.lo_signer,
                )
            )
            .where(
                OwnerAttestation.netuid == netuid,
                OwnerAttestation.lo_key_kind == "coldkey",
                OwnerAttestation.hi_key_kind == "coldkey",
                OwnerAttestation.created_at <= as_of,
                or_(
                    OwnerAttestation.revoked_at.is_(None),
                    OwnerAttestation.revoked_at > as_of,
                ),
                or_(
                    OwnerAttestation.lo_signer == coldkey,
                    OwnerAttestation.hi_signer == coldkey,
                ),
            )
            .limit(MAX_OWNER_LINKS + 1)
        )
    )
    if len(peers) > MAX_OWNER_LINKS:
        raise ProfileUnavailable("Observation exceeds the signed owner-link budget.")
    return {coldkey, *peers}


async def latest_paid_predecessor(
    session: AsyncSession, *, owners: set[str], as_of: datetime, candidate_id: UUID
) -> UUID | None:
    return await session.scalar(
        select(Agent.agent_id)
        .join(EvaluationPayment, EvaluationPayment.agent_id == Agent.agent_id)
        .where(
            EvaluationPayment.miner_coldkey.in_(owners),
            Agent.created_at <= as_of,
            Agent.agent_id != candidate_id,
        )
        .order_by(Agent.created_at.desc(), Agent.agent_id.desc())
        .limit(1)
    )


async def reference_feedback(session: AsyncSession, *, agent_id: UUID, as_of: datetime):
    screening = await session.scalar(
        select(ScreeningAttempt)
        .where(
            ScreeningAttempt.agent_id == agent_id, ScreeningAttempt.finished_at <= as_of
        )
        .order_by(
            ScreeningAttempt.finished_at.desc(), ScreeningAttempt.attempt_id.desc()
        )
        .limit(1)
    )
    failure = await session.scalar(
        select(ValidatorTicket)
        .where(
            ValidatorTicket.agent_id == agent_id,
            ValidatorTicket.failed_at <= as_of,
            ValidatorTicket.failure_reason.is_not(None),
            ValidatorTicket.purpose == TicketPurpose.CANONICAL_QUORUM,
        )
        .order_by(ValidatorTicket.failed_at.desc(), ValidatorTicket.validator_hotkey)
        .limit(1)
    )
    if (
        failure is not None
        and failure.failed_at is not None
        and (
            screening is None
            or screening.finished_at is None
            or failure.failed_at > screening.finished_at
        )
    ):
        return (
            "infrastructure"
            if failure.failure_reason == "infrastructure"
            else "pending",
            failure.failure_reason,
            failure.failed_at,
        )
    if screening is None:
        return "pending", None, None
    from ditto.db.queries.screening_infra_retry import INFRA_AUTO_RETRY_REASON_CODES

    if (
        screening.status == "failed"
        and screening.reason_code in INFRA_AUTO_RETRY_REASON_CODES
    ):
        return "infrastructure", screening.reason_code, screening.finished_at
    if screening.status in {"failed", "expired"}:
        return "pending", screening.reason_code, screening.finished_at
    if screening.status == "rejected":
        from ditto.api_server.submission_attempts import REPAIR_REASONS

        return (
            "repairable" if screening.reason_code in REPAIR_REASONS else "completed",
            screening.reason_code,
            screening.finished_at,
        )
    return "completed", screening.reason_code, screening.finished_at
