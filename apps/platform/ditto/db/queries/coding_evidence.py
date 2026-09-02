"""Append-only PostgreSQL authority for Hippius-mediated Coding evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.coding_evidence import (
    CodingSealedEvidenceIdentity,
    CodingSealedEvidenceKind,
)
from ditto.db.models import (
    CodingSealedEvidenceFinalization,
    CodingSealedEvidenceReservation,
    CodingShadowResult,
    CodingShadowRun,
    CodingShadowTicket,
)


class CodingSealedEvidenceNotAvailableError(RuntimeError):
    """The exact live ticket claim cannot authorize a new transition."""


class CodingSealedEvidenceConflictError(RuntimeError):
    """A reservation or finalization attempted to change immutable authority."""


@dataclass(frozen=True)
class CodingSealedEvidenceReservationResult:
    reservation: CodingSealedEvidenceReservation
    idempotent: bool


@dataclass(frozen=True)
class CodingSealedEvidenceFinalizationResult:
    finalization: CodingSealedEvidenceFinalization
    idempotent: bool


async def reserve_coding_sealed_evidence(
    session: AsyncSession,
    *,
    identity: CodingSealedEvidenceIdentity,
) -> CodingSealedEvidenceReservationResult:
    """Reserve one exact kind for the current started claim before storage I/O."""

    identity = _validated_identity(identity)
    await _require_live_started_claim(session, identity=identity)
    existing = await session.scalar(
        select(CodingSealedEvidenceReservation)
        .where(
            CodingSealedEvidenceReservation.ticket_id == identity.ticket_id,
            CodingSealedEvidenceReservation.claim_generation
            == identity.claim_generation,
            CodingSealedEvidenceReservation.evidence_kind
            == identity.evidence_kind.value,
        )
        .with_for_update()
    )
    if existing is not None:
        if _reservation_matches(existing, identity):
            return CodingSealedEvidenceReservationResult(existing, True)
        raise CodingSealedEvidenceConflictError(
            "coding evidence kind already reserves different immutable bytes"
        )

    inserted = await session.scalar(
        pg_insert(CodingSealedEvidenceReservation)
        .values(**_reservation_values(identity))
        .on_conflict_do_nothing(
            constraint=(
                "coding_sealed_evidence_reservations_ticket_generation_kind_key"
            )
        )
        .returning(CodingSealedEvidenceReservation.reservation_id)
    )
    if inserted is not None:
        reservation = await session.get(CodingSealedEvidenceReservation, inserted)
        if reservation is None:  # pragma: no cover - primary-key invariant
            raise RuntimeError("inserted coding evidence reservation was not readable")
        return CodingSealedEvidenceReservationResult(reservation, False)
    reservation = await session.scalar(
        select(CodingSealedEvidenceReservation).where(
            CodingSealedEvidenceReservation.ticket_id == identity.ticket_id,
            CodingSealedEvidenceReservation.claim_generation
            == identity.claim_generation,
            CodingSealedEvidenceReservation.evidence_kind
            == identity.evidence_kind.value,
        )
    )
    if reservation is None or not _reservation_matches(reservation, identity):
        raise CodingSealedEvidenceConflictError(
            "coding evidence kind already reserves different immutable bytes"
        )
    return CodingSealedEvidenceReservationResult(reservation, True)


async def finalize_coding_sealed_evidence(
    session: AsyncSession,
    *,
    identity: CodingSealedEvidenceIdentity,
    storage_status: str,
) -> CodingSealedEvidenceFinalizationResult:
    """Append finalization only after the mediator verified downloaded bytes."""

    identity = _validated_identity(identity)
    if storage_status not in {"uploaded", "reused"}:
        raise CodingSealedEvidenceConflictError(
            "coding evidence storage status is invalid"
        )
    reservation = await session.get(
        CodingSealedEvidenceReservation,
        identity.reservation_id,
        with_for_update=True,
    )
    existing = await session.get(
        CodingSealedEvidenceFinalization,
        identity.reservation_id,
        with_for_update=True,
    )
    if reservation is None or not _reservation_matches(reservation, identity):
        raise CodingSealedEvidenceConflictError(
            "coding evidence finalization disagrees with its reservation"
        )
    if existing is not None:
        if (
            existing.identity_sha256 == identity.identity_sha256
            and existing.weight_eligible is False
        ):
            return CodingSealedEvidenceFinalizationResult(existing, True)
        raise CodingSealedEvidenceConflictError(
            "coding evidence reservation already has another finalization"
        )

    await _require_live_started_claim(session, identity=identity)
    inserted = await session.scalar(
        pg_insert(CodingSealedEvidenceFinalization)
        .values(
            reservation_id=identity.reservation_id,
            identity_sha256=identity.identity_sha256,
            storage_status=storage_status,
            weight_eligible=False,
        )
        .on_conflict_do_nothing(constraint="coding_sealed_evidence_finalizations_pkey")
        .returning(CodingSealedEvidenceFinalization.reservation_id)
    )
    if inserted is not None:
        finalization = await session.get(CodingSealedEvidenceFinalization, inserted)
        if finalization is None:  # pragma: no cover - primary-key invariant
            raise RuntimeError("inserted coding evidence finalization was not readable")
        return CodingSealedEvidenceFinalizationResult(finalization, False)
    finalization = await session.get(
        CodingSealedEvidenceFinalization,
        identity.reservation_id,
    )
    if (
        finalization is None
        or finalization.identity_sha256 != identity.identity_sha256
        or finalization.weight_eligible is not False
    ):
        raise CodingSealedEvidenceConflictError(
            "coding evidence reservation already has another finalization"
        )
    return CodingSealedEvidenceFinalizationResult(finalization, True)


async def _require_live_started_claim(
    session: AsyncSession,
    *,
    identity: CodingSealedEvidenceIdentity,
) -> CodingShadowTicket:
    now = await session.scalar(select(func.clock_timestamp()))
    if not isinstance(now, datetime):  # pragma: no cover - DB invariant
        raise RuntimeError("database clock did not return a timestamp")
    now = _aware(now)
    ticket = await session.get(
        CodingShadowTicket,
        identity.ticket_id,
        with_for_update=True,
    )
    run = (
        await session.get(CodingShadowRun, ticket.run_row_id)
        if ticket is not None
        else None
    )
    terminal = await session.scalar(
        select(CodingShadowResult.result_id).where(
            CodingShadowResult.ticket_id == identity.ticket_id
        )
    )
    terminal_ack = (
        identity.evidence_kind
        is CodingSealedEvidenceKind.TERMINAL_PUBLICATION_ACKNOWLEDGEMENT
    )
    if (
        ticket is None
        or run is None
        or ticket.validator_hotkey != identity.validator_hotkey
        or ticket.claim_instance_id != identity.instance_id
        or ticket.claim_generation != identity.claim_generation
        or ticket.claim_started_at is None
        or ticket.claim_expires_at is None
        or _aware(ticket.deadline) != identity.ticket_deadline
        or _aware(ticket.deadline) <= now
        or _aware(ticket.claim_expires_at) <= now
        or (terminal_ack and terminal is None)
        or (not terminal_ack and terminal is not None)
        or ticket.task_count != 1
        or run.task_count != 1
        or run.coding_contract_version != 1
        or run.weight_eligible is not False
    ):
        raise CodingSealedEvidenceNotAvailableError(
            "coding evidence requires a live started ticket claim"
        )
    return ticket


def _validated_identity(
    identity: CodingSealedEvidenceIdentity,
) -> CodingSealedEvidenceIdentity:
    try:
        return CodingSealedEvidenceIdentity.model_validate_json(
            identity.model_dump_json(by_alias=True)
        )
    except (AttributeError, ValueError) as error:
        raise CodingSealedEvidenceConflictError(
            "coding evidence immutable identity is invalid"
        ) from error


def _reservation_values(identity: CodingSealedEvidenceIdentity) -> dict[str, object]:
    return {
        "reservation_id": identity.reservation_id,
        "ticket_id": identity.ticket_id,
        "claim_generation": identity.claim_generation,
        "validator_hotkey": identity.validator_hotkey,
        "instance_id": identity.instance_id,
        "ticket_deadline": identity.ticket_deadline,
        "evidence_kind": identity.evidence_kind.value,
        "plaintext_sha256": identity.plaintext_sha256,
        "plaintext_size_bytes": identity.plaintext_size_bytes,
        "ciphertext_sha256": identity.ciphertext_sha256,
        "ciphertext_size_bytes": identity.ciphertext_size_bytes,
        "object_key_sha256": identity.object_key_sha256,
        "envelope_sha256": identity.envelope_sha256,
        "wrapping_key_sha256": identity.wrapping_key_sha256,
        "aad_sha256": identity.aad_sha256,
        "identity_sha256": identity.identity_sha256,
        "weight_eligible": False,
    }


def _reservation_matches(
    reservation: CodingSealedEvidenceReservation,
    identity: CodingSealedEvidenceIdentity,
) -> bool:
    return all(
        (
            reservation.reservation_id == identity.reservation_id,
            reservation.ticket_id == identity.ticket_id,
            reservation.claim_generation == identity.claim_generation,
            reservation.validator_hotkey == identity.validator_hotkey,
            reservation.instance_id == identity.instance_id,
            _aware(reservation.ticket_deadline) == identity.ticket_deadline,
            reservation.evidence_kind == identity.evidence_kind.value,
            reservation.plaintext_sha256 == identity.plaintext_sha256,
            reservation.plaintext_size_bytes == identity.plaintext_size_bytes,
            reservation.ciphertext_sha256 == identity.ciphertext_sha256,
            reservation.ciphertext_size_bytes == identity.ciphertext_size_bytes,
            reservation.object_key_sha256 == identity.object_key_sha256,
            reservation.envelope_sha256 == identity.envelope_sha256,
            reservation.wrapping_key_sha256 == identity.wrapping_key_sha256,
            reservation.aad_sha256 == identity.aad_sha256,
            reservation.identity_sha256 == identity.identity_sha256,
            reservation.weight_eligible is False,
        )
    )


def _aware(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


__all__ = [
    "CodingSealedEvidenceConflictError",
    "CodingSealedEvidenceFinalizationResult",
    "CodingSealedEvidenceNotAvailableError",
    "CodingSealedEvidenceReservationResult",
    "finalize_coding_sealed_evidence",
    "reserve_coding_sealed_evidence",
]
