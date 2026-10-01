"""Authenticated observation ingress; no treasury spending endpoint."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.treasury_ingress import (
    TreasuryReceiptPage,
    TreasuryReceiptResult,
    TreasuryReceiptSelector,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.api_server.treasury_ingress import (
    ReceiptConflict,
    ingest_receipt,
    receipt_result,
)
from ditto.chain.errors import ChainError
from ditto.db.models import TreasuryVerifiedReceipt

router = APIRouter(prefix="/admin/treasury-receipts", tags=["admin"])


@router.get("", response_model=TreasuryReceiptPage)
async def list_treasury_receipts(
    _admin: Annotated[None, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> TreasuryReceiptPage:
    rows = list(
        await session.scalars(
            select(TreasuryVerifiedReceipt)
            .order_by(
                TreasuryVerifiedReceipt.recorded_at.desc(),
                TreasuryVerifiedReceipt.receipt_id,
            )
            .limit(limit)
        )
    )
    return TreasuryReceiptPage(
        items=[receipt_result(row, replayed=False) for row in rows]
    )


@router.post("", response_model=TreasuryReceiptResult)
async def record_treasury_receipt(
    payload: TreasuryReceiptSelector,
    request: Request,
    _admin: Annotated[None, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TreasuryReceiptResult:
    try:
        async with session.begin():
            return await ingest_receipt(session, request.app.state.chain, payload)
    except (ReceiptConflict, IntegrityError) as error:
        raise HTTPException(
            status_code=409, detail="treasury receipt conflicts with immutable history"
        ) from error
    except ValueError as error:
        # Validation exceptions may contain historical private settings. Return
        # a fixed refusal, never their raw values or provider transport details.
        raise HTTPException(
            status_code=422,
            detail="historical treasury receipt proof is invalid or unsupported",
        ) from error
    except ChainError as error:
        raise HTTPException(
            status_code=503, detail="finalized treasury receipt unavailable"
        ) from error
