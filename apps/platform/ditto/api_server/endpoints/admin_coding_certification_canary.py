"""Admin controls for the shadow coding-certification canary path.

The allowlist is a strict, append-only restriction: it refuses everything by
default and can admit only exact tuples, never global access, and it never
certifies anything itself. The lease listing is a read-only audit view that
never transitions a row.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.exc import IntegrityError as SAIntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.coding_certification import CodingCertificationStatus
from ditto.api_models.coding_certification_admin import (
    AdminCodingCertificationAllowlistApplyResponse,
    AdminCodingCertificationAllowlistRequest,
    AdminCodingCertificationAllowlistResponse,
    AdminCodingCertificationLeaseList,
    AdminCodingCertificationLeaseRecord,
    coding_certification_allowlist_confirmation,
)
from ditto.api_models.coding_certification_leases import (
    CodingCertificationLeaseStatus,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.db.queries.coding_certification_allowlist import (
    CodingCertificationAllowlistRevisionConflictError,
    active_coding_certification_allowlist,
    allowlist_revision_from_row,
    default_coding_certification_allowlist,
    insert_coding_certification_allowlist_revision,
    list_coding_certification_allowlist_revisions,
)
from ditto.db.queries.coding_certification_inference_grants import (
    revoke_unlisted_coding_certification_inference_grants,
)
from ditto.db.queries.coding_certification_leases import (
    abort_unlisted_coding_certification_leases,
    list_coding_certification_leases,
    receipt_window_ends_at,
    restamp_admitted_coding_certification_leases,
)

router = APIRouter(prefix="/admin", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]
_SS58 = r"^[1-9A-HJ-NP-Za-km-z]{47,48}$"


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def _allowlist_control(
    session: AsyncSession, *, history_limit: int
) -> AdminCodingCertificationAllowlistResponse:
    rows = await list_coding_certification_allowlist_revisions(
        session, limit=max(history_limit, 1)
    )
    current = (
        allowlist_revision_from_row(rows[0])
        if rows
        else default_coding_certification_allowlist()
    )
    return AdminCodingCertificationAllowlistResponse(
        enabled=current.effective == "exact_tuples",
        integrity=current.integrity,
        effective=current.effective,
        current=current,
        history=[allowlist_revision_from_row(row) for row in rows[:history_limit]],
    )


@router.get(
    "/coding-certification-allowlist",
    response_model=AdminCodingCertificationAllowlistResponse,
)
async def get_coding_certification_allowlist(
    response: Response,
    _admin: AdminDep,
    session: SessionDep,
    history_limit: Annotated[int, Query(ge=0, le=200)] = 50,
) -> AdminCodingCertificationAllowlistResponse:
    """Current restriction (revision 0 = built-in refuse-all) and history."""

    response.headers["Cache-Control"] = "no-store"
    return await _allowlist_control(session, history_limit=history_limit)


@router.post(
    "/coding-certification-allowlist",
    response_model=AdminCodingCertificationAllowlistApplyResponse,
    responses={
        409: {"description": "Stale expected_revision or concurrent write."},
        422: {"description": "Confirmation or entry shape is invalid."},
    },
)
async def set_coding_certification_allowlist(
    payload: AdminCodingCertificationAllowlistRequest,
    response: Response,
    _admin: AdminDep,
    session: SessionDep,
) -> AdminCodingCertificationAllowlistApplyResponse:
    """Append one complete revision, then abort and revoke what it refuses.

    In the same transaction as the new revision, every issued or claimed lease
    the revision does not admit is aborted (recording the revision) and every
    live certification inference grant it does not admit is revoked.
    """

    response.headers["Cache-Control"] = "no-store"
    expected = coding_certification_allowlist_confirmation(
        enabled=payload.enabled, entry_count=len(payload.entries)
    )
    if payload.confirmation != expected:
        raise HTTPException(
            status_code=422,
            detail=f'confirmation must equal "{expected}"',
        )
    try:
        async with session.begin():
            await insert_coding_certification_allowlist_revision(
                session,
                expected_revision=payload.expected_revision,
                enabled=payload.enabled,
                entries=payload.entries,
                reason=payload.reason,
                actor=payload.actor,
            )
            allowlist = await active_coding_certification_allowlist(session)
            aborted = await abort_unlisted_coding_certification_leases(
                session, allowlist=allowlist
            )
            await restamp_admitted_coding_certification_leases(
                session, allowlist=allowlist
            )
            revoked = await revoke_unlisted_coding_certification_inference_grants(
                session, allowlist=allowlist
            )
    except CodingCertificationAllowlistRevisionConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except SAIntegrityError as error:
        raise HTTPException(
            status_code=409,
            detail="coding certification allowlist changed concurrently; re-read it",
        ) from error
    control = await _allowlist_control(session, history_limit=50)
    return AdminCodingCertificationAllowlistApplyResponse(
        **control.model_dump(),
        aborted_lease_count=aborted,
        revoked_inference_grant_count=revoked,
    )


@router.get(
    "/coding-certification-leases",
    response_model=AdminCodingCertificationLeaseList,
)
async def list_coding_certification_lease_audit(
    response: Response,
    _admin: AdminDep,
    session: SessionDep,
    agent_id: UUID | None = None,
    validator_hotkey: Annotated[str | None, Query(pattern=_SS58)] = None,
    status: CodingCertificationLeaseStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AdminCodingCertificationLeaseList:
    """Newest-first certification lease rows without grant ids or bearer data."""

    response.headers["Cache-Control"] = "no-store"
    page = await list_coding_certification_leases(
        session,
        agent_id=agent_id,
        validator_hotkey=validator_hotkey,
        status=status,
        limit=limit,
        offset=offset,
    )
    return AdminCodingCertificationLeaseList(
        total=page.total,
        limit=limit,
        offset=offset,
        leases=[
            AdminCodingCertificationLeaseRecord(
                lease_id=row.lease.lease_id,
                agent_id=row.lease.agent_id,
                artifact_sha256=row.lease.artifact_sha256,
                screened_image_sha256=row.lease.screened_image_sha256,
                bench_version=row.lease.bench_version,
                coding_contract_version=row.lease.coding_contract_version,
                validator_hotkey=row.lease.validator_hotkey,
                status=CodingCertificationLeaseStatus(row.lease.status),
                issued_at=row.lease.issued_at,
                claimed_at=row.lease.claimed_at,
                aborted_at=row.lease.aborted_at,
                deadline=row.lease.deadline,
                deadline_passed=_aware(row.lease.deadline) <= page.now,
                receipt_window_ends_at=receipt_window_ends_at(row.lease),
                claim_allowlist_revision=row.lease.claim_allowlist_revision,
                aborted_allowlist_revision=row.lease.aborted_allowlist_revision,
                inference_grant_status=cast(
                    Literal["pending", "active", "revoked", "exhausted"] | None,
                    row.inference_grant_status,
                ),
                receipt_status=(
                    CodingCertificationStatus(row.receipt_status)
                    if row.receipt_status is not None
                    else None
                ),
            )
            for row in page.rows
        ],
    )
