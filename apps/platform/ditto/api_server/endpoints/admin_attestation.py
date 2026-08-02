"""Reviewer access to owner-link attestations, plus revocation.

Companion to ``GET /admin/miner-owners/{identifier}``, which infers ownership
from payment records. This endpoint reports the *proven* links instead. Both
are surfaced because they answer different questions and can legitimately
disagree: a miner can pay from a coldkey they no longer control, and can
control a key they never paid from.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.admin_attestation import (
    AdminAttestationRevokeRequest,
    AdminLinkedHotkey,
    AdminOwnerAttestation,
    AdminOwnerAttestationsResponse,
)
from ditto.api_server.attestation import evidence_grade, expected_netuid
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.db.models import OwnerAttestation
from ditto.db.queries.attestation import (
    list_attestations_for_hotkey,
    list_linked_hotkeys,
    revoke_attestation,
)

router = APIRouter(prefix="/admin/owner-attestations", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]


def _to_wire(row: OwnerAttestation, *, queried: str | None) -> AdminOwnerAttestation:
    counterparty = row.hotkey_hi if row.hotkey_lo == queried else row.hotkey_lo
    return AdminOwnerAttestation(
        attestation_id=row.attestation_id,
        netuid=row.netuid,
        hotkey_lo=row.hotkey_lo,
        hotkey_hi=row.hotkey_hi,
        counterparty=counterparty,
        evidence_grade=evidence_grade(row.lo_key_kind, row.hi_key_kind),  # type: ignore[arg-type]
        lo_key_kind=row.lo_key_kind,  # type: ignore[arg-type]
        lo_signer=row.lo_signer,
        hi_key_kind=row.hi_key_kind,  # type: ignore[arg-type]
        hi_signer=row.hi_signer,
        nonce=row.nonce,
        issued_at=row.issued_at,
        created_at=row.created_at,
        revoked_at=row.revoked_at,
        revoked_by=row.revoked_by,
        revoked_reason=row.revoked_reason,
        active=row.revoked_at is None,
    )


@router.get("/{hotkey}", response_model=AdminOwnerAttestationsResponse)
async def get_owner_attestations(
    hotkey: str,
    _: AdminDep,
    session: SessionDep,
) -> AdminOwnerAttestationsResponse:
    """Every attestation naming ``hotkey``, plus its currently-linked hotkeys.

    An unknown hotkey returns empty lists rather than a 404: "this miner has
    never attested anything" is an answer a reviewer needs, and it is the
    answer that matters most when a miner claims otherwise.
    """
    netuid = expected_netuid()
    rows = await list_attestations_for_hotkey(session, hotkey=hotkey, netuid=netuid)
    linked = await list_linked_hotkeys(session, hotkey=hotkey, netuid=netuid)
    return AdminOwnerAttestationsResponse(
        hotkey=hotkey,
        netuid=netuid,
        attestations=[_to_wire(row, queried=hotkey) for row in rows],
        linked_hotkeys=[
            AdminLinkedHotkey(
                hotkey=link.hotkey,
                attestation_id=link.attestation_id,
                evidence_grade=link.grade,  # type: ignore[arg-type]
            )
            for link in linked
        ],
    )


@router.post("/{attestation_id}/revoke", response_model=AdminOwnerAttestation)
async def revoke_owner_attestation(
    attestation_id: UUID,
    body: AdminAttestationRevokeRequest,
    _: AdminDep,
    session: SessionDep,
    x_admin_actor: Annotated[str | None, Header()] = None,
) -> AdminOwnerAttestation:
    """Stop an attestation from counting, from now on.

    Prospective only. Submissions already screened while the link was active
    keep their decision: screening is a point-in-time adjudication, and
    re-holding already-scored agents on a later revocation would punish
    reliance and destabilise the ledger. The row is retained, so the window
    during which the link was live stays auditable.

    Creating a link needs both endpoints; withdrawing one needs only one of
    them, or an operator here.
    """
    if x_admin_actor is None or not 1 <= len(x_admin_actor) <= 120:
        raise HTTPException(status_code=422, detail="X-Admin-Actor is required")
    async with session.begin():
        row = await revoke_attestation(
            session,
            attestation_id=attestation_id,
            revoked_by=x_admin_actor,
            reason=body.reason,
        )
        if row is None:
            raise HTTPException(
                status_code=404, detail="no active attestation with that id"
            )
        result = _to_wire(row, queried=None)
    return result
