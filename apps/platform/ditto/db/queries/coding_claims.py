"""Exclusive, non-rerunnable worker claims for shadow coding tickets."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.db.models import (
    Agent,
    CodingCapabilityCertification,
    CodingShadowAuthoringFreeze,
    CodingShadowResult,
    CodingShadowRun,
    CodingShadowTicket,
)

_INSTANCE = re.compile(r"^[^\s\x00-\x1f\x7f]{1,128}$")
_VALIDATOR = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{47,48}$")
_CLAIM_TTL = timedelta(minutes=2)
_MINIMUM_REMAINING = timedelta(seconds=30)


class CodingClaimNotAvailableError(RuntimeError):
    pass


class CodingClaimConflictError(RuntimeError):
    pass


@dataclass(frozen=True)
class CodingTicketClaim:
    ticket: CodingShadowTicket
    run: CodingShadowRun
    instance_id: str
    idempotent: bool


async def claim_next_coding_ticket(
    session: AsyncSession,
    *,
    validator_hotkey: str,
    instance_id: str,
    run_row_id: UUID,
) -> CodingTicketClaim | None:
    """Claim or renew the one ticket owned by a stable worker instance."""

    _validate_authority(validator_hotkey, instance_id, run_row_id)
    await _lock_instance(session, validator_hotkey, instance_id)
    now = await _database_now(session)
    current = await session.scalar(
        select(CodingShadowTicket)
        .where(
            CodingShadowTicket.validator_hotkey == validator_hotkey,
            CodingShadowTicket.claim_instance_id == instance_id,
        )
        .order_by(CodingShadowTicket.ticket_id)
        .with_for_update()
        .limit(1)
    )
    if current is not None:
        if current.run_row_id != run_row_id:
            raise CodingClaimConflictError(
                "coding worker instance is bound to another run"
            )
        if await _has_terminal_result(session, current.ticket_id):
            _clear_claim(current)
            await session.flush()
        elif current.deadline > now and (
            current.claim_started_at is not None
            or (current.claim_expires_at is not None and current.claim_expires_at > now)
        ):
            result = await _claim_result(session, current, instance_id, True)
            if result is not None:
                _renew(current, now)
                await session.flush()
                return result
            if current.claim_started_at is not None:
                return None
            _clear_claim(current)
            await session.flush()
        else:
            # Deadline passed: the ticket cannot be retried, so release the
            # instance slot even if authoring had already started.
            _clear_claim(current)
            await session.flush()

    terminal = exists().where(
        CodingShadowResult.ticket_id == CodingShadowTicket.ticket_id
    )
    frozen = exists().where(
        CodingShadowAuthoringFreeze.ticket_id == CodingShadowTicket.ticket_id
    )
    ticket = await session.scalar(
        select(CodingShadowTicket)
        .join(
            CodingShadowRun, CodingShadowRun.run_row_id == CodingShadowTicket.run_row_id
        )
        .join(
            CodingCapabilityCertification,
            CodingCapabilityCertification.certification_row_id
            == CodingShadowTicket.certification_row_id,
        )
        .join(Agent, Agent.agent_id == CodingShadowRun.agent_id)
        .where(
            CodingShadowTicket.validator_hotkey == validator_hotkey,
            CodingShadowTicket.run_row_id == run_row_id,
            CodingShadowTicket.deadline > now + _MINIMUM_REMAINING,
            ~terminal,
            ~frozen,
            or_(
                CodingShadowTicket.claim_instance_id.is_(None),
                and_(
                    CodingShadowTicket.claim_started_at.is_(None),
                    CodingShadowTicket.claim_expires_at <= now,
                ),
            ),
            CodingShadowRun.weight_eligible.is_(False),
            CodingShadowRun.coding_contract_version == 1,
            CodingShadowRun.task_count == 1,
            CodingShadowTicket.task_count == 1,
            Agent.sha256 == CodingShadowRun.artifact_sha256,
            Agent.screened_image_sha256 == CodingShadowRun.screened_image_sha256,
            CodingCapabilityCertification.status == "certified",
            CodingCapabilityCertification.settlement_generation.is_not(None),
            CodingCapabilityCertification.validator_hotkey == validator_hotkey,
            CodingCapabilityCertification.agent_id == CodingShadowRun.agent_id,
            CodingCapabilityCertification.artifact_sha256
            == CodingShadowRun.artifact_sha256,
            CodingCapabilityCertification.screened_image_sha256
            == CodingShadowRun.screened_image_sha256,
            CodingCapabilityCertification.coding_contract_version == 1,
            CodingCapabilityCertification.bench_version
            == CodingShadowRun.bench_version,
            CodingCapabilityCertification.ticket_deadline
            >= CodingShadowTicket.deadline,
            CodingCapabilityCertification.expires_at > CodingShadowTicket.deadline,
        )
        .order_by(CodingShadowTicket.issued_at, CodingShadowTicket.ticket_id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if ticket is None:
        return None
    ticket.claim_generation += 1
    ticket.claim_instance_id = instance_id
    ticket.claim_acquired_at = now
    ticket.claim_heartbeat_at = now
    ticket.claim_expires_at = _expiry(ticket, now)
    ticket.claim_started_at = None
    result = await _claim_result(session, ticket, instance_id, False)
    if result is None:  # pragma: no cover - selected by the same authority joins
        raise CodingClaimConflictError("coding claim authority changed")
    await session.flush()
    return result


async def start_coding_ticket_claim(
    session: AsyncSession,
    *,
    validator_hotkey: str,
    instance_id: str,
    run_row_id: UUID,
    ticket_id: UUID,
    claim_generation: int,
) -> CodingTicketClaim:
    return await _update_claim(
        session,
        validator_hotkey=validator_hotkey,
        instance_id=instance_id,
        run_row_id=run_row_id,
        ticket_id=ticket_id,
        claim_generation=claim_generation,
        start=True,
    )


async def heartbeat_coding_ticket_claim(
    session: AsyncSession,
    *,
    validator_hotkey: str,
    instance_id: str,
    run_row_id: UUID,
    ticket_id: UUID,
    claim_generation: int,
) -> CodingTicketClaim:
    return await _update_claim(
        session,
        validator_hotkey=validator_hotkey,
        instance_id=instance_id,
        run_row_id=run_row_id,
        ticket_id=ticket_id,
        claim_generation=claim_generation,
        start=False,
    )


async def _update_claim(
    session: AsyncSession,
    *,
    validator_hotkey: str,
    instance_id: str,
    run_row_id: UUID,
    ticket_id: UUID,
    claim_generation: int,
    start: bool,
) -> CodingTicketClaim:
    _validate_authority(validator_hotkey, instance_id, run_row_id)
    if claim_generation < 1 or claim_generation > (1 << 31) - 1:
        raise CodingClaimConflictError("coding claim generation is invalid")
    await _lock_instance(session, validator_hotkey, instance_id)
    now = await _database_now(session)
    ticket = await session.get(CodingShadowTicket, ticket_id, with_for_update=True)
    if (
        ticket is None
        or ticket.run_row_id != run_row_id
        or ticket.validator_hotkey != validator_hotkey
        or ticket.claim_instance_id != instance_id
        or ticket.claim_generation != claim_generation
        or ticket.claim_acquired_at is None
        or ticket.claim_heartbeat_at is None
        or ticket.claim_expires_at is None
        or ticket.deadline <= now
        or ticket.claim_expires_at <= now
        or await _has_terminal_result(session, ticket_id)
    ):
        raise CodingClaimNotAvailableError("coding ticket claim is unavailable")
    idempotent = False
    if start:
        idempotent = ticket.claim_started_at is not None
        if ticket.claim_started_at is None:
            ticket.claim_started_at = now
    ticket.claim_heartbeat_at = now
    ticket.claim_expires_at = _expiry(ticket, now)
    result = await _claim_result(session, ticket, instance_id, idempotent)
    if result is None:
        raise CodingClaimConflictError("coding ticket claim authority changed")
    await session.flush()
    return result


async def _claim_result(
    session: AsyncSession,
    ticket: CodingShadowTicket,
    instance_id: str,
    idempotent: bool,
) -> CodingTicketClaim | None:
    run = await session.get(CodingShadowRun, ticket.run_row_id)
    certification = await session.get(
        CodingCapabilityCertification, ticket.certification_row_id
    )
    agent = await session.get(Agent, run.agent_id) if run is not None else None
    if (
        run is None
        or certification is None
        or agent is None
        or run.coding_contract_version != 1
        or run.task_count != 1
        or ticket.task_count != 1
        or run.weight_eligible
        or agent.sha256 != run.artifact_sha256
        or agent.screened_image_sha256 != run.screened_image_sha256
        or certification.status != "certified"
        or certification.settlement_generation is None
        or certification.validator_hotkey != ticket.validator_hotkey
        or certification.agent_id != run.agent_id
        or certification.artifact_sha256 != run.artifact_sha256
        or certification.screened_image_sha256 != run.screened_image_sha256
        or certification.coding_contract_version != 1
        or certification.bench_version != run.bench_version
        or certification.ticket_deadline < ticket.deadline
        or certification.expires_at <= ticket.deadline
    ):
        return None
    return CodingTicketClaim(
        ticket=ticket,
        run=run,
        instance_id=instance_id,
        idempotent=idempotent,
    )


async def _has_terminal_result(session: AsyncSession, ticket_id) -> bool:
    value = await session.scalar(
        select(CodingShadowResult.result_id).where(
            CodingShadowResult.ticket_id == ticket_id
        )
    )
    return value is not None


def _renew(ticket: CodingShadowTicket, now: datetime) -> None:
    ticket.claim_heartbeat_at = now
    ticket.claim_expires_at = _expiry(ticket, now)


def _expiry(ticket: CodingShadowTicket, now: datetime) -> datetime:
    return min(now + _CLAIM_TTL, ticket.deadline)


def _clear_claim(ticket: CodingShadowTicket) -> None:
    ticket.claim_instance_id = None
    ticket.claim_acquired_at = None
    ticket.claim_heartbeat_at = None
    ticket.claim_expires_at = None
    ticket.claim_started_at = None


async def _database_now(session: AsyncSession) -> datetime:
    value = await session.scalar(select(func.clock_timestamp()))
    if not isinstance(value, datetime):  # pragma: no cover - DB invariant
        raise RuntimeError("database clock did not return a timestamp")
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


async def _lock_instance(
    session: AsyncSession, validator_hotkey: str, instance_id: str
) -> None:
    key = f"coding-shadow-claim:{validator_hotkey}:{instance_id}"
    await session.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended(key, 0)))
    )


def _validate_authority(
    validator_hotkey: str, instance_id: str, run_row_id: UUID
) -> None:
    if (
        _VALIDATOR.fullmatch(validator_hotkey) is None
        or _INSTANCE.fullmatch(instance_id) is None
        or len(instance_id.encode()) > 128
        or run_row_id.int == 0
    ):
        raise CodingClaimConflictError("coding claim identity is invalid")


__all__ = [
    "CodingClaimConflictError",
    "CodingClaimNotAvailableError",
    "CodingTicketClaim",
    "claim_next_coding_ticket",
    "heartbeat_coding_ticket_claim",
    "start_coding_ticket_claim",
]
