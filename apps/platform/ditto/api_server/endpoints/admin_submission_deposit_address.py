"""Audited Backroom-only control for the miner submission deposit address."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.submission_deposit_address import (
    AdminSubmissionDepositAddressRequest,
    AdminSubmissionDepositAddressResponse,
)
from ditto.api_models.submission_deposit_address import (
    SubmissionDepositAddressRevision as RevisionModel,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.db.models import (
    SubmissionDepositAddressRevision,
    UploadAdmissionReservation,
)
from ditto.db.queries.submission_deposit_address import (
    latest_submission_deposit_address,
)

router = APIRouter(prefix="/admin/submission-deposit-address", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]


def _revision(row: SubmissionDepositAddressRevision) -> RevisionModel:
    return RevisionModel(
        revision=row.revision,
        parent_revision=row.parent_revision,
        payment_address=row.payment_address,
        reason=row.reason,
        actor=row.actor,
        created_at=row.created_at,
    )


def _default_revision(address: str) -> RevisionModel:
    return RevisionModel(
        revision=0,
        parent_revision=0,
        payment_address=address,
        reason="Boot-configured submission deposit address",
        actor="platform",
        created_at=None,
    )


async def _response(
    session: AsyncSession, *, default_address: str
) -> AdminSubmissionDepositAddressResponse:
    rows = list(
        await session.scalars(
            select(SubmissionDepositAddressRevision)
            .order_by(SubmissionDepositAddressRevision.revision.desc())
            .limit(100)
        )
    )
    return AdminSubmissionDepositAddressResponse(
        current=_revision(rows[0]) if rows else _default_revision(default_address),
        history=[_revision(row) for row in rows],
    )


@router.get("", response_model=AdminSubmissionDepositAddressResponse)
async def get_submission_deposit_address(
    request: Request, _admin: AdminDep, session: SessionDep
) -> AdminSubmissionDepositAddressResponse:
    return await _response(
        session, default_address=request.app.state.config.upload_payment_address
    )


@router.post("", response_model=AdminSubmissionDepositAddressResponse)
async def set_submission_deposit_address(
    request: Request,
    payload: AdminSubmissionDepositAddressRequest,
    _admin: AdminDep,
    session: SessionDep,
) -> AdminSubmissionDepositAddressResponse:
    expected_confirmation = f"SET SUBMISSION DEPOSIT ADDRESS {payload.payment_address}"
    if payload.confirmation != expected_confirmation:
        raise HTTPException(
            status_code=409,
            detail=f"confirmation must be exactly {expected_confirmation}",
        )

    default_address = request.app.state.config.upload_payment_address
    try:
        async with session.begin():
            # Address rotation must not change the destination promised by a
            # pre-payment reservation. Block concurrent reservation writes,
            # snapshot the old effective address into legacy rows, then append
            # the new revision in the same transaction.
            if session.get_bind().dialect.name == "postgresql":
                await session.execute(
                    text(
                        "LOCK TABLE upload_admission_reservations "
                        "IN SHARE ROW EXCLUSIVE MODE"
                    )
                )
            latest = await latest_submission_deposit_address(session)
            actual_revision = latest.revision if latest is not None else 0
            if payload.expected_revision != actual_revision:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "submission deposit address changed; refresh before applying "
                        f"(expected {payload.expected_revision}, "
                        f"current {actual_revision})"
                    ),
                )
            old_address = (
                latest.payment_address if latest is not None else default_address
            )
            await session.execute(
                update(UploadAdmissionReservation)
                .where(UploadAdmissionReservation.payment_send_address.is_(None))
                .values(payment_send_address=old_address)
            )
            session.add(
                SubmissionDepositAddressRevision(
                    parent_revision=actual_revision,
                    payment_address=payload.payment_address,
                    reason=payload.reason.strip(),
                    actor=payload.actor.strip(),
                )
            )
    except IntegrityError as error:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail=(
                "submission deposit address changed concurrently; "
                "refresh before applying"
            ),
        ) from error

    return await _response(session, default_address=default_address)
