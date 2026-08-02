"""Miner-facing owner-link attestation submission.

Unauthenticated in the header sense, like the rest of the miner surface: the
two sr25519 proofs in the body *are* the authentication. There is nothing to
authorise here beyond proving control of both endpoints, which the payload
does.

See :mod:`ditto.api_server.attestation` for what is signed and why, including
the third-party-hotkey attack that requiring both halves defends against.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.attestation import (
    OwnerLinkRequest,
    OwnerLinkResponse,
)
from ditto.api_server.attestation import (
    AttestationRejected,
    canonical_pair,
    evidence_grade,
    verify_link,
)
from ditto.api_server.dependencies import get_session
from ditto.db.queries.attestation import (
    AttestationReplayedError,
    clear_linked_pending_copy_reviews,
    get_bound_coldkey_for_hotkey,
    record_attestation,
)

router = APIRouter(prefix="/attestations", tags=["attestations"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]


@router.post("/owner-link", response_model=OwnerLinkResponse, status_code=201)
async def submit_owner_link(
    body: OwnerLinkRequest,
    session: SessionDep,
) -> OwnerLinkResponse:
    """Record a proven same-operator link between two hotkeys.

    Both halves must verify. Each may be proved by its own hotkey or by the
    coldkey bound to that hotkey through payment records. Neither hotkey is
    required to be currently registered on the subnet -- see
    :func:`ditto.api_server.attestation.verify_link`.
    """
    now = datetime.now(UTC)
    hotkey_lo, hotkey_hi = canonical_pair(body.hotkey_a, body.hotkey_b)
    # The caller may pass the pair in either order; bind each proof to the side
    # its hotkey actually landed on so the signed `side` field cannot drift
    # from the stored column.
    lo_proof = body.proof_a if body.hotkey_a == hotkey_lo else body.proof_b
    hi_proof = body.proof_b if body.hotkey_a == hotkey_lo else body.proof_a

    # One transaction for the whole handler: the coldkey-binding reads must see
    # the same snapshot the uniqueness checks below run against, so a hotkey
    # cannot change hands between being verified and being recorded.
    async with session.begin():
        # A coldkey proof is checked against a binding the platform learned
        # from on-chain payment proofs, never from this request.
        lo_bound = await get_bound_coldkey_for_hotkey(session, hotkey=hotkey_lo)
        hi_bound = await get_bound_coldkey_for_hotkey(session, hotkey=hotkey_hi)

        try:
            verify_link(
                netuid=body.netuid,
                hotkey_lo=hotkey_lo,
                hotkey_hi=hotkey_hi,
                nonce=body.nonce,
                issued_at=body.issued_at,
                lo_key_kind=lo_proof.key_kind,
                lo_signer=lo_proof.signer,
                lo_signature=lo_proof.signature,
                hi_key_kind=hi_proof.key_kind,
                hi_signer=hi_proof.signer,
                hi_signature=hi_proof.signature,
                lo_bound_coldkey=lo_bound,
                hi_bound_coldkey=hi_bound,
                now=now,
            )
        except AttestationRejected as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

        try:
            row = await record_attestation(
                session,
                netuid=body.netuid,
                hotkey_lo=hotkey_lo,
                hotkey_hi=hotkey_hi,
                nonce=body.nonce,
                issued_at=body.issued_at,
                lo_key_kind=lo_proof.key_kind,
                lo_signer=lo_proof.signer,
                lo_signature=lo_proof.signature.lower(),
                hi_key_kind=hi_proof.key_kind,
                hi_signer=hi_proof.signer,
                hi_signature=hi_proof.signature.lower(),
            )
        except AttestationReplayedError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        cleared_review_ids = await clear_linked_pending_copy_reviews(
            session,
            hotkey_lo=hotkey_lo,
            hotkey_hi=hotkey_hi,
            attestation_id=row.attestation_id,
            now=now,
        )
        created_at = row.created_at or now
        attestation_id = row.attestation_id

    return OwnerLinkResponse(
        attestation_id=attestation_id,
        netuid=body.netuid,
        hotkey_lo=hotkey_lo,
        hotkey_hi=hotkey_hi,
        evidence_grade=evidence_grade(lo_proof.key_kind, hi_proof.key_kind),
        created_at=created_at,
        cleared_copy_review_count=len(cleared_review_ids),
    )
