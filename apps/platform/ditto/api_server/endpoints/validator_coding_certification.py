"""Validator persistence for shadow-only coding capability certifications."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.coding_certification import (
    CodingCertificationStatus,
    SubmitCodingCertificationRequest,
    SubmitCodingCertificationResponse,
    coding_certification_signing_message,
)
from ditto.api_server.attestation import verify_signature
from ditto.api_server.dependencies import get_chain_client, get_session
from ditto.api_server.endpoints.validator import (
    ValidatorAuthError,
    _assert_validator_permitted,
)
from ditto.chain import ChainClient
from ditto.db.models import Agent
from ditto.db.queries.coding_certification_allowlist import (
    CODING_CERTIFICATION_NOT_ALLOWLISTED,
    CodingCertificationAllowlistRefusedError,
    active_coding_certification_allowlist,
)
from ditto.db.queries.coding_certification_leases import (
    CodingCertificationLeaseNotAvailableError,
    complete_coding_certification_lease,
    database_now,
    expire_coding_certification_lease_if_due,
    lock_coding_certification_lease,
)
from ditto.db.queries.coding_certifications import (
    CodingCertificationConflictError,
    CodingCertificationSettlementError,
    coding_certification_lease_accepts_receipt,
    coding_certification_matches,
    coding_certification_settlement_bound,
    get_coding_certification_by_lease,
    get_coding_certification_identity,
    insert_coding_certification,
)

router = APIRouter(prefix="/validator", tags=["validator"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
ChainDep = Annotated[ChainClient, Depends(get_chain_client)]

_MAX_ISSUED_AT_SKEW = timedelta(minutes=5)
_LEASE_UNAVAILABLE = "coding certification lease is not available"


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


@router.post(
    "/agent/{agent_id}/coding-certification",
    response_model=SubmitCodingCertificationResponse,
    responses={
        401: {"description": "Signature invalid or validator not permitted."},
        403: {"description": "The certification allowlist refuses the lease."},
        404: {"description": "Agent or live lease not found."},
        409: {"description": "Artifact, lease, receipt, or replay conflict."},
    },
)
async def submit_coding_certification(
    agent_id: UUID,
    payload: SubmitCodingCertificationRequest,
    request: Request,
    response: Response,
    chain: ChainDep,
    session: SessionDep,
) -> SubmitCodingCertificationResponse:
    """Append one signed shadow receipt without touching score state."""

    response.headers["Cache-Control"] = "no-store"
    receipt = payload.receipt
    signed = coding_certification_signing_message(
        validator_hotkey=payload.validator_hotkey,
        agent_id=agent_id,
        bench_version=payload.bench_version,
        lease_id=payload.lease_id,
        screened_image_sha256=payload.screened_image_sha256,
        certification_sha256=receipt.certification_sha256,
    )
    if not verify_signature(
        signer=payload.validator_hotkey,
        payload=signed,
        signature_hex=payload.signature,
    ):
        raise ValidatorAuthError("coding certification signature did not verify")

    netuid = request.app.state.config.chain.netuid
    network = request.app.state.config.chain.subtensor_network
    await _assert_validator_permitted(
        chain, netuid, payload.validator_hotkey, network=network
    )

    try:
        issued_at = datetime.fromtimestamp(receipt.issued_at_unix, UTC)
        expires_at = datetime.fromtimestamp(receipt.expires_at_unix, UTC)
    except (OverflowError, OSError, ValueError) as error:
        raise HTTPException(
            status_code=409,
            detail=(
                "coding certification receipt timestamps are outside supported bounds"
            ),
        ) from error
    outcome = await _submit_coding_certification(
        agent_id=agent_id,
        payload=payload,
        session=session,
        issued_at=issued_at,
        expires_at=expires_at,
    )
    if not isinstance(outcome, SubmitCodingCertificationResponse):
        # The late receipt is refused, and the lease expiry (releasing its
        # in-flight slot and revoking any live grant) has already committed.
        raise HTTPException(status_code=404, detail=_LEASE_UNAVAILABLE)
    return outcome


def _receipt_agent(
    agent: Agent | None, payload: SubmitCodingCertificationRequest
) -> Agent:
    if agent is None:
        raise HTTPException(status_code=404, detail="agent not found")
    if payload.receipt.agent_artifact_sha256 != agent.sha256:
        raise HTTPException(
            status_code=409,
            detail="coding receipt artifact does not match the agent",
        )
    if (
        agent.screened_image_sha256 is None
        or payload.screened_image_sha256 != agent.screened_image_sha256
    ):
        raise HTTPException(
            status_code=409,
            detail="coding receipt screened image is absent or stale",
        )
    return agent


async def _receipt_replay(
    session: AsyncSession,
    *,
    agent: Agent,
    payload: SubmitCodingCertificationRequest,
) -> SubmitCodingCertificationResponse | None:
    """The stored answer for an exact replay of an accepted receipt, if any."""

    receipt = payload.receipt
    existing = await get_coding_certification_identity(
        session,
        agent_id=agent.agent_id,
        validator_hotkey=payload.validator_hotkey,
        coding_contract_version=receipt.coding_contract_version,
        certification_id=receipt.certification_id,
    )
    if existing is None:
        return None
    if not coding_certification_matches(
        existing,
        artifact_sha256=agent.sha256,
        screened_image_sha256=payload.screened_image_sha256,
        bench_version=payload.bench_version,
        lease_id=payload.lease_id,
        ticket_deadline=_aware(existing.ticket_deadline),
        receipt=receipt,
    ):
        raise HTTPException(
            status_code=409,
            detail="coding certification identity names different evidence",
        )
    if (
        receipt.status is CodingCertificationStatus.CERTIFIED
        and not coding_certification_settlement_bound(existing)
    ):
        raise HTTPException(
            status_code=409,
            detail="certified receipt lacks durable settlement binding",
        )
    now = await database_now(session)
    return SubmitCodingCertificationResponse(
        agent_id=agent.agent_id,
        certification_id=receipt.certification_id,
        status=receipt.status,
        accepted=True,
        idempotent=True,
        active=(
            receipt.status is CodingCertificationStatus.CERTIFIED
            and coding_certification_settlement_bound(existing)
            and _aware(existing.expires_at) > now
        ),
    )


async def _submit_coding_certification(
    *,
    agent_id: UUID,
    payload: SubmitCodingCertificationRequest,
    session: AsyncSession,
    issued_at: datetime,
    expires_at: datetime,
) -> SubmitCodingCertificationResponse | Literal["late"]:
    """Accept one receipt, or expire its late lease and commit that expiry.

    Lock order is allowlist, agent, lease, grant. The shared allowlist lock is
    taken first and a new receipt's tuple is decided before the agent row is
    locked, so a refused caller never waits on or holds that row. An exact
    replay of an accepted receipt stays idempotent and is answered before the
    allowlist decision, because it writes nothing. Every deadline decision uses
    the database clock read after the lease row lock.
    """

    receipt = payload.receipt
    async with session.begin():
        allowlist = await active_coding_certification_allowlist(session)
        agent = _receipt_agent(await session.get(Agent, agent_id), payload)
        replay = await _receipt_replay(session, agent=agent, payload=payload)
        if replay is not None:
            return replay
        if not allowlist.admits(
            agent_id=agent_id,
            artifact_sha256=agent.sha256,
            screened_image_sha256=agent.screened_image_sha256,
            validator_hotkey=payload.validator_hotkey,
        ):
            # Nothing was written; no receipt row exists for a refused tuple.
            raise HTTPException(
                status_code=403, detail=CODING_CERTIFICATION_NOT_ALLOWLISTED
            )
        agent = _receipt_agent(
            await session.get(
                Agent, agent_id, with_for_update=True, populate_existing=True
            ),
            payload,
        )
        # An identical submission may have committed while this one waited.
        replay = await _receipt_replay(session, agent=agent, payload=payload)
        if replay is not None:
            return replay

        by_lease = await get_coding_certification_by_lease(
            session, lease_id=payload.lease_id
        )
        if by_lease is not None:
            raise HTTPException(
                status_code=409,
                detail="coding certification identity names different evidence",
            )
        try:
            gate = await lock_coding_certification_lease(
                session,
                lease_id=payload.lease_id,
                validator_hotkey=payload.validator_hotkey,
            )
        except CodingCertificationLeaseNotAvailableError:
            raise HTTPException(status_code=404, detail=_LEASE_UNAVAILABLE) from None
        except CodingCertificationAllowlistRefusedError:
            raise HTTPException(
                status_code=403, detail=CODING_CERTIFICATION_NOT_ALLOWLISTED
            ) from None
        lease, now = gate.lease, gate.now
        # The one receipt deadline decision: an issued lease is late at its
        # deadline, a claimed one only after its receipt window.
        if await expire_coding_certification_lease_if_due(
            session, lease=lease, now=now
        ):
            return "late"
        if issued_at > now + _MAX_ISSUED_AT_SKEW or expires_at <= now:
            raise HTTPException(
                status_code=409,
                detail="coding certification receipt is not currently active",
            )
        if not coding_certification_lease_accepts_receipt(
            lease,
            validator_hotkey=payload.validator_hotkey,
            agent_id=agent_id,
            artifact_sha256=agent.sha256,
            screened_image_sha256=payload.screened_image_sha256,
            bench_version=payload.bench_version,
            receipt=receipt,
        ):
            raise HTTPException(
                status_code=409,
                detail="no matching claimed certification lease",
            )
        lease_issued_at = _aware(lease.issued_at)
        lease_deadline = _aware(lease.deadline)
        if (
            issued_at < lease_issued_at - _MAX_ISSUED_AT_SKEW
            or issued_at > lease_deadline
        ):
            raise HTTPException(
                status_code=409,
                detail="coding certification receipt predates or postdates its lease",
            )
        try:
            result = await insert_coding_certification(
                session,
                agent_id=agent_id,
                artifact_sha256=agent.sha256,
                screened_image_sha256=payload.screened_image_sha256,
                validator_hotkey=payload.validator_hotkey,
                bench_version=payload.bench_version,
                lease_id=payload.lease_id,
                ticket_deadline=lease_deadline,
                receipt=receipt,
                signature=payload.signature,
            )
        except (
            CodingCertificationConflictError,
            CodingCertificationSettlementError,
        ) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        complete_coding_certification_lease(lease)
        await session.flush()

    return SubmitCodingCertificationResponse(
        agent_id=agent_id,
        certification_id=receipt.certification_id,
        status=receipt.status,
        accepted=True,
        idempotent=result.idempotent,
        active=(
            receipt.status is CodingCertificationStatus.CERTIFIED
            and coding_certification_settlement_bound(result.row)
            and _aware(result.row.expires_at) > now
        ),
    )
