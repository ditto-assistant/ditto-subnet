"""Miner-facing signed handle claims.

Unauthenticated in the header sense, like owner-link attestations: the
sr25519 proof in the body *is* the authentication.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.name_claim import (
    NameClaimEndorsementView,
    NameClaimListResponse,
    NameClaimRequest,
    NameClaimResponse,
    NameEndorseRequest,
    NameWithdrawRequest,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.name_claim import (
    ENDORSEMENT_THRESHOLD,
    ENTRENCHMENT_AGE,
    NameClaimRejected,
    check_freshness,
    claim_message,
    endorse_message,
    expected_netuid,
    require_name_stem,
    verify_signed_action,
    withdraw_message,
)
from ditto.db.models import NameClaim, NameClaimEndorsement
from ditto.db.queries.attestation import get_bound_coldkey_for_hotkey
from ditto.db.queries.name_claims import (
    NameClaimConflictError,
    NameClaimReplayedError,
    claimant_has_used_stem,
    get_name_claim,
    list_endorsements,
    list_entrenched_owner_roots,
    list_name_claims,
    owner_root_for_hotkey,
    record_endorsement,
    record_name_claim,
    withdraw_name_claim,
)

router = APIRouter(prefix="/name-claims", tags=["name-claims"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]


def _to_response(
    claim: NameClaim,
    endorsements: list[NameClaimEndorsement],
) -> NameClaimResponse:
    return NameClaimResponse(
        claim_id=claim.claim_id,
        netuid=claim.netuid,
        name_stem=claim.name_stem,
        claimant_hotkey=claim.claimant_hotkey,
        status=claim.status,  # type: ignore[arg-type]
        endorsement_count=len(endorsements),
        endorsement_threshold=ENDORSEMENT_THRESHOLD,
        endorsements=[
            NameClaimEndorsementView(
                endorsement_id=row.endorsement_id,
                endorser_hotkey=row.endorser_hotkey,
                created_at=row.created_at,
            )
            for row in endorsements
        ],
        created_at=claim.created_at,
        upheld_at=claim.upheld_at,
        withdrawn_at=claim.withdrawn_at,
    )


async def _responses_for(
    session: AsyncSession, claims: list[NameClaim]
) -> list[NameClaimResponse]:
    by_claim = await list_endorsements(
        session, claim_ids=[claim.claim_id for claim in claims]
    )
    return [_to_response(claim, by_claim.get(claim.claim_id, [])) for claim in claims]


@router.get("", response_model=NameClaimListResponse)
async def list_claims(session: SessionDep) -> NameClaimListResponse:
    """List live handle claims for this deployment."""
    now = datetime.now(UTC)
    async with session.begin():
        claims = await list_name_claims(session, netuid=expected_netuid())
        items = await _responses_for(session, claims)
    return NameClaimListResponse(
        generated_at=now,
        endorsement_threshold=ENDORSEMENT_THRESHOLD,
        claims=items,
    )


@router.get("/{claim_id}", response_model=NameClaimResponse)
async def get_claim(claim_id: UUID, session: SessionDep) -> NameClaimResponse:
    async with session.begin():
        claim = await get_name_claim(session, claim_id=claim_id)
        if claim is None:
            raise HTTPException(status_code=404, detail="name claim not found")
        items = await _responses_for(session, [claim])
    return items[0]


@router.post("", response_model=NameClaimResponse, status_code=201)
async def submit_claim(
    body: NameClaimRequest,
    session: SessionDep,
) -> NameClaimResponse:
    """Record a signed handle claim. Starts ``pending`` until endorsed."""
    now = datetime.now(UTC)
    if body.netuid != expected_netuid():
        raise HTTPException(
            status_code=400,
            detail=f"name claim was minted for netuid {body.netuid}, "
            f"this deployment serves {expected_netuid()}",
        )
    try:
        stem = require_name_stem(body.name)
    except NameClaimRejected as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    async with session.begin():
        bound = await get_bound_coldkey_for_hotkey(session, hotkey=body.claimant_hotkey)
        payload = claim_message(
            netuid=body.netuid,
            name_stem=stem,
            claimant_hotkey=body.claimant_hotkey,
            nonce=body.nonce,
            issued_at=body.issued_at,
            key_kind=body.proof.key_kind,
            signer=body.proof.signer,
        )
        try:
            check_freshness(issued_at=body.issued_at, now=now)
            verify_signed_action(
                payload=payload,
                hotkey=body.claimant_hotkey,
                key_kind=body.proof.key_kind,
                signer=body.proof.signer,
                signature=body.proof.signature,
                bound_coldkey=bound,
            )
        except NameClaimRejected as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if not await claimant_has_used_stem(
            session, claimant_hotkey=body.claimant_hotkey, name_stem=stem
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"claimant has no existing submission whose name collapses "
                    f"to handle {stem!r}"
                ),
            )
        try:
            row = await record_name_claim(
                session,
                netuid=body.netuid,
                name_stem=stem,
                claimant_hotkey=body.claimant_hotkey,
                claimant_key_kind=body.proof.key_kind,
                claimant_signer=body.proof.signer,
                claimant_signature=body.proof.signature.lower(),
                nonce=body.nonce,
                issued_at=body.issued_at,
            )
        except NameClaimReplayedError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except NameClaimConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        created_at = row.created_at or now
        claim_id = row.claim_id
        claimant_hotkey = row.claimant_hotkey
        status = row.status

    return NameClaimResponse(
        claim_id=claim_id,
        netuid=body.netuid,
        name_stem=stem,
        claimant_hotkey=claimant_hotkey,
        status=status,  # type: ignore[arg-type]
        endorsement_count=0,
        endorsement_threshold=ENDORSEMENT_THRESHOLD,
        endorsements=[],
        created_at=created_at,
    )


@router.post(
    "/{claim_id}/endorsements",
    response_model=NameClaimResponse,
    status_code=201,
)
async def submit_endorsement(
    claim_id: UUID,
    body: NameEndorseRequest,
    session: SessionDep,
) -> NameClaimResponse:
    """Add an entrenched-miner endorsement. May uphold the claim."""
    now = datetime.now(UTC)
    if body.netuid != expected_netuid():
        raise HTTPException(
            status_code=400,
            detail=f"endorsement was minted for netuid {body.netuid}, "
            f"this deployment serves {expected_netuid()}",
        )
    async with session.begin():
        claim = await get_name_claim(session, claim_id=claim_id, for_update=True)
        if claim is None:
            raise HTTPException(status_code=404, detail="name claim not found")
        if claim.status == "withdrawn":
            raise HTTPException(status_code=409, detail="name claim has been withdrawn")
        if claim.name_stem != body.name_stem:
            raise HTTPException(
                status_code=400,
                detail="endorsement name_stem does not match the claim",
            )
        bound = await get_bound_coldkey_for_hotkey(session, hotkey=body.endorser_hotkey)
        payload = endorse_message(
            netuid=body.netuid,
            claim_id=claim_id,
            name_stem=body.name_stem,
            endorser_hotkey=body.endorser_hotkey,
            nonce=body.nonce,
            issued_at=body.issued_at,
            key_kind=body.proof.key_kind,
            signer=body.proof.signer,
        )
        try:
            check_freshness(issued_at=body.issued_at, now=now)
            verify_signed_action(
                payload=payload,
                hotkey=body.endorser_hotkey,
                key_kind=body.proof.key_kind,
                signer=body.proof.signer,
                signature=body.proof.signature,
                bound_coldkey=bound,
            )
        except NameClaimRejected as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        claimant_root = await owner_root_for_hotkey(
            session, hotkey=claim.claimant_hotkey
        )
        endorser_root = await owner_root_for_hotkey(
            session, hotkey=body.endorser_hotkey
        )
        if endorser_root == claimant_root:
            raise HTTPException(
                status_code=400,
                detail="a miner cannot endorse their own handle claim",
            )
        entrenched = await list_entrenched_owner_roots(session, now=now)
        if endorser_root not in entrenched:
            raise HTTPException(
                status_code=400,
                detail=(
                    "endorser is not an entrenched miner family: need a "
                    "full-benchmark scored submission whose family is at "
                    f"least {int(ENTRENCHMENT_AGE.days)} days old"
                ),
            )
        try:
            _endorsement, claim = await record_endorsement(
                session,
                claim=claim,
                endorser_hotkey=body.endorser_hotkey,
                endorser_owner_root=endorser_root,
                endorser_key_kind=body.proof.key_kind,
                endorser_signer=body.proof.signer,
                endorser_signature=body.proof.signature.lower(),
                nonce=body.nonce,
                issued_at=body.issued_at,
                now=now,
            )
        except NameClaimReplayedError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except NameClaimConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        items = await _responses_for(session, [claim])
    return items[0]


@router.post("/{claim_id}/withdraw", response_model=NameClaimResponse)
async def submit_withdrawal(
    claim_id: UUID,
    body: NameWithdrawRequest,
    session: SessionDep,
) -> NameClaimResponse:
    """Release a reserved or pending stem. Must be signed by the claimant."""
    now = datetime.now(UTC)
    if body.netuid != expected_netuid():
        raise HTTPException(
            status_code=400,
            detail=f"withdrawal was minted for netuid {body.netuid}, "
            f"this deployment serves {expected_netuid()}",
        )
    async with session.begin():
        claim = await get_name_claim(session, claim_id=claim_id, for_update=True)
        if claim is None:
            raise HTTPException(status_code=404, detail="name claim not found")
        if claim.status == "withdrawn":
            raise HTTPException(
                status_code=409, detail="name claim is already withdrawn"
            )
        if body.claimant_hotkey != claim.claimant_hotkey:
            raise HTTPException(
                status_code=400,
                detail="withdrawal must be signed by the original claimant hotkey",
            )
        bound = await get_bound_coldkey_for_hotkey(session, hotkey=body.claimant_hotkey)
        payload = withdraw_message(
            netuid=body.netuid,
            claim_id=claim_id,
            name_stem=claim.name_stem,
            claimant_hotkey=body.claimant_hotkey,
            nonce=body.nonce,
            issued_at=body.issued_at,
            key_kind=body.proof.key_kind,
            signer=body.proof.signer,
        )
        try:
            check_freshness(issued_at=body.issued_at, now=now)
            verify_signed_action(
                payload=payload,
                hotkey=body.claimant_hotkey,
                key_kind=body.proof.key_kind,
                signer=body.proof.signer,
                signature=body.proof.signature,
                bound_coldkey=bound,
            )
        except NameClaimRejected as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        claim = await withdraw_name_claim(
            session,
            claim=claim,
            withdrawn_signer=body.proof.signer,
            withdrawn_signature=body.proof.signature.lower(),
            now=now,
        )
        items = await _responses_for(session, [claim])
    return items[0]
