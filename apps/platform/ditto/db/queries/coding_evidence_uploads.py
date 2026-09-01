"""Append-only sealed-evidence reservation and finalization authority.

This module is intentionally storage-agnostic.  It records only the immutable
identity which a later, separately reviewed Platform storage finalizer has
verified.  It never accepts an object key, an object-store URL, or credentials.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.coding_evidence_upload import (
    CODING_SEALED_EVIDENCE_MAX_BYTES,
    CodingSealedEvidenceKind,
)
from ditto.db.models import (
    CodingSealedEvidenceFinalization,
    CodingSealedEvidenceUpload,
    CodingShadowResult,
    CodingShadowRun,
    CodingShadowTicket,
)

_INSTANCE = re.compile(r"^[^\s\x00-\x1f\x7f]{1,128}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_VALIDATOR = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{47,48}$")


class CodingSealedEvidenceNotAvailableError(RuntimeError):
    """The signed worker no longer owns a live started ticket claim."""


class CodingSealedEvidenceConflictError(RuntimeError):
    """A sealed-evidence identity attempted to change immutable bytes."""


@dataclass(frozen=True)
class CodingSealedEvidenceUploadReservation:
    upload: CodingSealedEvidenceUpload
    ticket: CodingShadowTicket
    idempotent: bool


@dataclass(frozen=True)
class CodingSealedEvidenceFinalizationResult:
    finalization: CodingSealedEvidenceFinalization
    idempotent: bool


@dataclass(frozen=True)
class CodingSealedEvidenceFinalizationAuthority:
    upload: CodingSealedEvidenceUpload
    ticket: CodingShadowTicket


async def replay_coding_sealed_evidence_finalization(
    session: AsyncSession,
    *,
    validator_hotkey: str,
    instance_id: str,
    ticket_id: UUID,
    claim_generation: int,
    upload_id: UUID,
    evidence_kind: CodingSealedEvidenceKind,
    sha256: str,
    size_bytes: int,
) -> CodingSealedEvidenceFinalizationResult | None:
    """Return one exact terminal-ack receipt without reviving claim authority.

    This path exists only for the crash window after Platform committed the
    finalization but before the validator committed its local outbox release.
    It cannot create a finalization, mint a capability, or authorize execution.
    """

    _validate_claim_identity(
        validator_hotkey=validator_hotkey,
        instance_id=instance_id,
        claim_generation=claim_generation,
    )
    kind, sha256, size_bytes = _validate_evidence_identity(
        evidence_kind=evidence_kind,
        sha256=sha256,
        size_bytes=size_bytes,
    )
    if kind != CodingSealedEvidenceKind.TERMINAL_PUBLICATION_ACKNOWLEDGEMENT.value:
        return None
    ticket = await session.get(CodingShadowTicket, ticket_id, with_for_update=True)
    upload = await session.get(
        CodingSealedEvidenceUpload, upload_id, with_for_update=True
    )
    finalization = await session.get(
        CodingSealedEvidenceFinalization,
        upload_id,
        with_for_update=True,
    )
    if finalization is None:
        return None
    if (
        ticket is None
        or upload is None
        or ticket.validator_hotkey != validator_hotkey
        or ticket.ticket_id != ticket_id
        or ticket.claim_generation != claim_generation
        or (
            ticket.claim_instance_id is not None
            and ticket.claim_instance_id != instance_id
        )
        or upload.ticket_id != ticket_id
        or upload.claim_generation != claim_generation
        or upload.evidence_kind != kind
        or not _upload_matches(upload, sha256=sha256, size_bytes=size_bytes)
        or not _finalization_matches(
            finalization,
            ticket_id=ticket_id,
            claim_generation=claim_generation,
            evidence_kind=kind,
            sha256=sha256,
            size_bytes=size_bytes,
        )
    ):
        raise CodingSealedEvidenceConflictError(
            "coding evidence finalized receipt replay conflicts"
        )
    return CodingSealedEvidenceFinalizationResult(
        finalization=finalization,
        idempotent=True,
    )


async def reserve_coding_sealed_evidence_upload(
    session: AsyncSession,
    *,
    validator_hotkey: str,
    instance_id: str,
    ticket_id: UUID,
    claim_generation: int,
    evidence_kind: CodingSealedEvidenceKind,
    sha256: str,
    size_bytes: int,
) -> CodingSealedEvidenceUploadReservation:
    """Reserve one exact evidence kind for the currently started claim.

    A ticket claim generation can reserve a kind exactly once.  The exact same
    request replays safely; a different digest or size is a permanent conflict.
    The generated upload ID is Platform authority, not caller input.
    """

    kind, sha256, size_bytes = _validate_evidence_identity(
        evidence_kind=evidence_kind,
        sha256=sha256,
        size_bytes=size_bytes,
    )
    ticket = await _require_live_started_claim(
        session,
        validator_hotkey=validator_hotkey,
        instance_id=instance_id,
        ticket_id=ticket_id,
        claim_generation=claim_generation,
        terminal_result_required=(
            kind == CodingSealedEvidenceKind.TERMINAL_PUBLICATION_ACKNOWLEDGEMENT.value
        ),
    )
    existing = await session.scalar(
        select(CodingSealedEvidenceUpload)
        .where(
            CodingSealedEvidenceUpload.ticket_id == ticket.ticket_id,
            CodingSealedEvidenceUpload.claim_generation == claim_generation,
            CodingSealedEvidenceUpload.evidence_kind == kind,
        )
        .with_for_update()
    )
    if existing is not None:
        if _upload_matches(existing, sha256=sha256, size_bytes=size_bytes):
            return CodingSealedEvidenceUploadReservation(
                upload=existing,
                ticket=ticket,
                idempotent=True,
            )
        raise CodingSealedEvidenceConflictError(
            "coding evidence kind already reserves different immutable bytes"
        )

    inserted_id = await session.scalar(
        pg_insert(CodingSealedEvidenceUpload)
        .values(
            upload_id=uuid4(),
            ticket_id=ticket.ticket_id,
            claim_generation=claim_generation,
            evidence_kind=kind,
            sha256=sha256,
            size_bytes=size_bytes,
            content_type="application/octet-stream",
            weight_eligible=False,
        )
        .on_conflict_do_nothing(
            constraint="coding_sealed_evidence_uploads_ticket_generation_kind_key"
        )
        .returning(CodingSealedEvidenceUpload.upload_id)
    )
    if inserted_id is not None:
        upload = await session.get(CodingSealedEvidenceUpload, inserted_id)
        if upload is None:  # pragma: no cover - database primary-key invariant
            raise RuntimeError("inserted coding evidence upload was not readable")
        return CodingSealedEvidenceUploadReservation(
            upload=upload,
            ticket=ticket,
            idempotent=False,
        )
    upload = await session.scalar(
        select(CodingSealedEvidenceUpload).where(
            CodingSealedEvidenceUpload.ticket_id == ticket.ticket_id,
            CodingSealedEvidenceUpload.claim_generation == claim_generation,
            CodingSealedEvidenceUpload.evidence_kind == kind,
        )
    )
    if upload is None or not _upload_matches(
        upload, sha256=sha256, size_bytes=size_bytes
    ):
        raise CodingSealedEvidenceConflictError(
            "coding evidence kind already reserves different immutable bytes"
        )
    return CodingSealedEvidenceUploadReservation(
        upload=upload,
        ticket=ticket,
        idempotent=True,
    )


async def authorize_coding_sealed_evidence_finalization(
    session: AsyncSession,
    *,
    validator_hotkey: str,
    instance_id: str,
    ticket_id: UUID,
    claim_generation: int,
    upload_id: UUID,
    evidence_kind: CodingSealedEvidenceKind,
    sha256: str,
    size_bytes: int,
) -> CodingSealedEvidenceFinalizationAuthority:
    """Lock and return the exact live reservation before storage verification."""

    kind, sha256, size_bytes = _validate_evidence_identity(
        evidence_kind=evidence_kind,
        sha256=sha256,
        size_bytes=size_bytes,
    )
    ticket = await _require_live_started_claim(
        session,
        validator_hotkey=validator_hotkey,
        instance_id=instance_id,
        ticket_id=ticket_id,
        claim_generation=claim_generation,
        terminal_result_required=(
            kind == CodingSealedEvidenceKind.TERMINAL_PUBLICATION_ACKNOWLEDGEMENT.value
        ),
    )
    upload = await session.get(
        CodingSealedEvidenceUpload,
        upload_id,
        with_for_update=True,
    )
    if upload is None:
        raise CodingSealedEvidenceNotAvailableError(
            "coding evidence upload reservation is unavailable"
        )
    if (
        upload.ticket_id != ticket.ticket_id
        or upload.claim_generation != claim_generation
        or upload.evidence_kind != kind
        or not _upload_matches(upload, sha256=sha256, size_bytes=size_bytes)
    ):
        raise CodingSealedEvidenceConflictError(
            "coding evidence finalization disagrees with its reservation"
        )
    return CodingSealedEvidenceFinalizationAuthority(upload=upload, ticket=ticket)


async def finalize_coding_sealed_evidence_upload(
    session: AsyncSession,
    *,
    validator_hotkey: str,
    instance_id: str,
    ticket_id: UUID,
    claim_generation: int,
    upload_id: UUID,
    evidence_kind: CodingSealedEvidenceKind,
    sha256: str,
    size_bytes: int,
) -> CodingSealedEvidenceFinalizationResult:
    """Append the accepted finalization after a future storage verifier runs.

    This function deliberately receives only that verifier's exact immutable
    identity.  It neither verifies nor learns an object-store location; the
    following S3 integration layer must do that before it invokes this ledger.
    """

    authority = await authorize_coding_sealed_evidence_finalization(
        session,
        validator_hotkey=validator_hotkey,
        instance_id=instance_id,
        ticket_id=ticket_id,
        claim_generation=claim_generation,
        upload_id=upload_id,
        evidence_kind=evidence_kind,
        sha256=sha256,
        size_bytes=size_bytes,
    )
    upload, ticket = authority.upload, authority.ticket
    kind = evidence_kind.value

    existing = await session.get(
        CodingSealedEvidenceFinalization,
        upload_id,
        with_for_update=True,
    )
    if existing is not None:
        if _finalization_matches(
            existing,
            ticket_id=ticket.ticket_id,
            claim_generation=claim_generation,
            evidence_kind=kind,
            sha256=sha256,
            size_bytes=size_bytes,
        ):
            return CodingSealedEvidenceFinalizationResult(
                finalization=existing,
                idempotent=True,
            )
        raise CodingSealedEvidenceConflictError(
            "coding evidence upload already finalized with different authority"
        )

    inserted_id = await session.scalar(
        pg_insert(CodingSealedEvidenceFinalization)
        .values(
            upload_id=upload.upload_id,
            ticket_id=ticket.ticket_id,
            claim_generation=claim_generation,
            evidence_kind=kind,
            sha256=sha256,
            size_bytes=size_bytes,
            weight_eligible=False,
        )
        .on_conflict_do_nothing(constraint="coding_sealed_evidence_finalizations_pkey")
        .returning(CodingSealedEvidenceFinalization.upload_id)
    )
    if inserted_id is not None:
        finalization = await session.get(CodingSealedEvidenceFinalization, inserted_id)
        if finalization is None:  # pragma: no cover - primary-key invariant
            raise RuntimeError("inserted coding evidence finalization was not readable")
        return CodingSealedEvidenceFinalizationResult(
            finalization=finalization,
            idempotent=False,
        )
    finalization = await session.get(CodingSealedEvidenceFinalization, upload_id)
    if finalization is None or not _finalization_matches(
        finalization,
        ticket_id=ticket.ticket_id,
        claim_generation=claim_generation,
        evidence_kind=kind,
        sha256=sha256,
        size_bytes=size_bytes,
    ):
        raise CodingSealedEvidenceConflictError(
            "coding evidence upload already finalized with different authority"
        )
    return CodingSealedEvidenceFinalizationResult(
        finalization=finalization,
        idempotent=True,
    )


async def _require_live_started_claim(
    session: AsyncSession,
    *,
    validator_hotkey: str,
    instance_id: str,
    ticket_id: UUID,
    claim_generation: int,
    terminal_result_required: bool,
) -> CodingShadowTicket:
    _validate_claim_identity(
        validator_hotkey=validator_hotkey,
        instance_id=instance_id,
        claim_generation=claim_generation,
    )
    now = await _database_now(session)
    ticket = await session.get(CodingShadowTicket, ticket_id, with_for_update=True)
    terminal = await session.scalar(
        select(CodingShadowResult.result_id).where(
            CodingShadowResult.ticket_id == ticket_id
        )
    )
    run = (
        await session.get(CodingShadowRun, ticket.run_row_id)
        if ticket is not None
        else None
    )
    if (
        ticket is None
        or run is None
        or ticket.validator_hotkey != validator_hotkey
        or ticket.claim_instance_id != instance_id
        or ticket.claim_generation != claim_generation
        or ticket.claim_started_at is None
        or ticket.claim_expires_at is None
        or ticket.deadline <= now
        or ticket.claim_expires_at <= now
        or (terminal_result_required and terminal is None)
        or (not terminal_result_required and terminal is not None)
        or ticket.task_count != 1
        or run.task_count != 1
        or run.coding_contract_version != 1
        or run.weight_eligible
    ):
        raise CodingSealedEvidenceNotAvailableError(
            "coding evidence upload requires a live started ticket claim"
        )
    return ticket


def _validate_claim_identity(
    *, validator_hotkey: str, instance_id: str, claim_generation: int
) -> None:
    if (
        _VALIDATOR.fullmatch(validator_hotkey) is None
        or _INSTANCE.fullmatch(instance_id) is None
        or len(instance_id.encode()) > 128
        or claim_generation < 1
        or claim_generation > (1 << 31) - 1
    ):
        raise CodingSealedEvidenceConflictError(
            "coding evidence claim identity is invalid"
        )


def _validate_evidence_identity(
    *,
    evidence_kind: CodingSealedEvidenceKind,
    sha256: str,
    size_bytes: int,
) -> tuple[str, str, int]:
    if not isinstance(evidence_kind, CodingSealedEvidenceKind):
        raise CodingSealedEvidenceConflictError("coding evidence kind is invalid")
    if (
        not isinstance(sha256, str)
        or _SHA256.fullmatch(sha256) is None
        or not isinstance(size_bytes, int)
        or isinstance(size_bytes, bool)
        or not 1 <= size_bytes <= CODING_SEALED_EVIDENCE_MAX_BYTES[evidence_kind]
    ):
        raise CodingSealedEvidenceConflictError(
            "coding evidence immutable identity is invalid"
        )
    return evidence_kind.value, sha256, size_bytes


def _upload_matches(
    upload: CodingSealedEvidenceUpload, *, sha256: str, size_bytes: int
) -> bool:
    return (
        upload.sha256 == sha256
        and upload.size_bytes == size_bytes
        and upload.content_type == "application/octet-stream"
        and upload.weight_eligible is False
    )


def _finalization_matches(
    finalization: CodingSealedEvidenceFinalization,
    *,
    ticket_id: UUID,
    claim_generation: int,
    evidence_kind: str,
    sha256: str,
    size_bytes: int,
) -> bool:
    return (
        finalization.ticket_id == ticket_id
        and finalization.claim_generation == claim_generation
        and finalization.evidence_kind == evidence_kind
        and finalization.sha256 == sha256
        and finalization.size_bytes == size_bytes
        and finalization.weight_eligible is False
    )


async def _database_now(session: AsyncSession) -> datetime:
    value = await session.scalar(select(func.clock_timestamp()))
    if not isinstance(value, datetime):  # pragma: no cover - DB invariant
        raise RuntimeError("database clock did not return a timestamp")
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


__all__ = [
    "CodingSealedEvidenceConflictError",
    "CodingSealedEvidenceFinalizationAuthority",
    "CodingSealedEvidenceFinalizationResult",
    "CodingSealedEvidenceNotAvailableError",
    "CodingSealedEvidenceUploadReservation",
    "authorize_coding_sealed_evidence_finalization",
    "finalize_coding_sealed_evidence_upload",
    "replay_coding_sealed_evidence_finalization",
    "reserve_coding_sealed_evidence_upload",
]
