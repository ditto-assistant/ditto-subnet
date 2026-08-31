"""Default-off operator entry point for one k=3 shadow coding ticket set."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.exc import IntegrityError as SAIntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.coding_evaluation import (
    AdminCodingShadowTicketRecord,
    AdminCodingShadowTicketSetRequest,
    AdminCodingShadowTicketSetResponse,
)
from ditto.api_server.dependencies import get_chain_client, get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.chain import ChainClient
from ditto.db.models import CodingShadowTicket
from ditto.db.queries.coding_evaluations import (
    CodingShadowConflictError,
    CodingShadowNotQualifiedError,
)
from ditto.db.queries.coding_ticket_sets import (
    CodingTicketSetPolicy,
    CodingTicketSetUnavailableError,
    issue_coding_shadow_ticket_set,
)

router = APIRouter(prefix="/admin/coding-shadow/ticket-sets", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
ChainDep = Annotated[ChainClient, Depends(get_chain_client)]
AdminDep = Annotated[None, Depends(require_admin)]


def _confirmation(payload: AdminCodingShadowTicketSetRequest) -> str:
    validators = ",".join(payload.validator_hotkeys)
    return (
        "ISSUE SHADOW CODING TICKET SET "
        f"{payload.run_row_id} {payload.ticket_set_id} {validators}"
    )


def _ticket_record(ticket: CodingShadowTicket) -> AdminCodingShadowTicketRecord:
    return AdminCodingShadowTicketRecord(
        ticket_id=ticket.ticket_id,
        validator_hotkey=ticket.validator_hotkey,
        issued_at=ticket.issued_at,
        deadline=ticket.deadline,
    )


@router.post(
    "",
    response_model=AdminCodingShadowTicketSetResponse,
    responses={
        401: {"description": "Admin authentication failed."},
        409: {"description": "Run, validator, or immutable authority conflict."},
        503: {"description": "Ticket-set issuance is disabled or unavailable."},
    },
)
async def issue_coding_shadow_tickets(
    payload: AdminCodingShadowTicketSetRequest,
    request: Request,
    response: Response,
    _admin: AdminDep,
    chain: ChainDep,
    session: SessionDep,
) -> AdminCodingShadowTicketSetResponse:
    """Issue one named k=3 set without selecting validators or running work."""

    response.headers["Cache-Control"] = "no-store"
    config = request.app.state.config
    if not config.coding_shadow_ticket_set_enabled:
        raise HTTPException(
            status_code=503,
            detail="coding shadow ticket-set issuance is disabled",
        )
    expected_confirmation = _confirmation(payload)
    if payload.confirmation != expected_confirmation:
        raise HTTPException(
            status_code=422,
            detail=f'confirmation must equal "{expected_confirmation}"',
        )
    try:
        async with session.begin():
            result = await issue_coding_shadow_ticket_set(
                session,
                permit_source=chain,
                netuid=config.chain.netuid,
                run_row_id=payload.run_row_id,
                ticket_set_id=payload.ticket_set_id,
                validator_hotkeys=payload.validator_hotkeys,
                policy=CodingTicketSetPolicy(
                    lease_seconds=config.coding_shadow_ticket_lease_seconds,
                ),
            )
    except CodingTicketSetUnavailableError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except (
        CodingShadowConflictError,
        CodingShadowNotQualifiedError,
    ) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except SAIntegrityError as error:
        raise HTTPException(
            status_code=409,
            detail="coding shadow ticket-set issuance changed concurrently",
        ) from error
    return AdminCodingShadowTicketSetResponse(
        run_row_id=payload.run_row_id,
        ticket_set_id=payload.ticket_set_id,
        tickets=(
            _ticket_record(result.tickets[0]),
            _ticket_record(result.tickets[1]),
            _ticket_record(result.tickets[2]),
        ),
        idempotent=result.idempotent,
    )
