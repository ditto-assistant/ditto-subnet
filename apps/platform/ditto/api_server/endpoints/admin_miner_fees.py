"""Read-only admin accounting for miner upload fees."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy import case, distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.admin_miner_fees import AdminMinerFeeSummary, MinerFeeDay
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.db.models import EvaluationPayment

router = APIRouter(prefix="/admin/miner-fees", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]
_RAO_PER_TAO = Decimal("1000000000")


def _usd_value() -> object:
    return EvaluationPayment.amount_rao * EvaluationPayment.tao_usd_rate / _RAO_PER_TAO


@router.get("", response_model=AdminMinerFeeSummary)
async def get_miner_fee_summary(
    request: Request,
    _: AdminDep,
    session: SessionDep,
) -> AdminMinerFeeSummary:
    """Return gross revenue recorded by replay-protected payment proofs."""
    generated_at = datetime.now(UTC)
    priced_count = func.sum(
        case((EvaluationPayment.tao_usd_rate.is_not(None), 1), else_=0)
    )
    totals = (
        await session.execute(
            select(
                func.count(EvaluationPayment.block_hash),
                func.coalesce(func.sum(EvaluationPayment.amount_rao), 0),
                func.coalesce(priced_count, 0),
                func.coalesce(func.sum(_usd_value()), 0),
                func.count(distinct(EvaluationPayment.miner_coldkey)),
                func.min(EvaluationPayment.timestamp),
                func.max(EvaluationPayment.timestamp),
            )
        )
    ).one()

    day = func.date(EvaluationPayment.timestamp).label("day")
    recent = (
        await session.execute(
            select(
                day,
                func.count(EvaluationPayment.block_hash),
                func.sum(EvaluationPayment.amount_rao),
                priced_count,
                func.coalesce(func.sum(_usd_value()), 0),
            )
            .where(EvaluationPayment.timestamp >= generated_at - timedelta(days=30))
            .group_by(day)
            .order_by(day)
        )
    ).all()

    paid_submissions = int(totals[0])
    priced_submissions = int(totals[2])
    return AdminMinerFeeSummary(
        generated_at=generated_at,
        payment_address=request.app.state.config.upload_payment_address,
        paid_submissions=paid_submissions,
        gross_amount_rao=int(totals[1]),
        priced_submissions=priced_submissions,
        unpriced_submissions=paid_submissions - priced_submissions,
        gross_value_usd=Decimal(totals[3]),
        unique_paying_coldkeys=int(totals[4]),
        first_payment_at=totals[5],
        last_payment_at=totals[6],
        recent_days=[
            MinerFeeDay(
                date=row[0],
                paid_submissions=int(row[1]),
                gross_amount_rao=int(row[2]),
                priced_submissions=int(row[3]),
                gross_value_usd=Decimal(row[4]),
            )
            for row in recent
        ],
    )
