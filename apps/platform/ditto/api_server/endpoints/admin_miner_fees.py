"""Read-only admin accounting for miner upload fees."""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import case, distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.admin_miner_fees import (
    AdminMinerFeeSummary,
    MinerFeeAddress,
    MinerFeeDay,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.db.models import EvaluationPayment, SubmissionDepositAddressRevision
from ditto.db.queries.submission_deposit_address import (
    effective_submission_deposit_address,
)

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
    payment_address = await effective_submission_deposit_address(
        session, default_address=request.app.state.config.upload_payment_address
    )
    address_rows = (
        await session.execute(
            select(
                EvaluationPayment.dest_address,
                func.count(EvaluationPayment.block_hash),
                func.sum(EvaluationPayment.amount_rao),
                priced_count,
                func.coalesce(func.sum(_usd_value()), 0),
                func.min(EvaluationPayment.timestamp),
                func.max(EvaluationPayment.timestamp),
            ).group_by(EvaluationPayment.dest_address)
        )
    ).all()
    addresses = {
        row[0]: MinerFeeAddress(
            payment_address=row[0],
            is_current=row[0] == payment_address,
            paid_submissions=int(row[1]),
            gross_amount_rao=int(row[2]),
            priced_submissions=int(row[3]),
            gross_value_usd=Decimal(row[4]),
            first_payment_at=row[5],
            last_payment_at=row[6],
        )
        for row in address_rows
    }
    # Revisions can include addresses that never received an accepted payment.
    # The deployment fallback is a known destination, not an activation date.
    configured = await session.scalars(
        select(SubmissionDepositAddressRevision.payment_address).distinct()
    )
    for address in {
        payment_address,
        request.app.state.config.upload_payment_address,
        *configured,
    }:
        if address not in addresses:
            addresses[address] = MinerFeeAddress(
                payment_address=address,
                is_current=address == payment_address,
                paid_submissions=0,
                gross_amount_rao=0,
                priced_submissions=0,
                gross_value_usd=Decimal(0),
                first_payment_at=None,
                last_payment_at=None,
            )
    return AdminMinerFeeSummary(
        generated_at=generated_at,
        payment_address=payment_address,
        paid_submissions=paid_submissions,
        gross_amount_rao=int(totals[1]),
        priced_submissions=priced_submissions,
        unpriced_submissions=paid_submissions - priced_submissions,
        gross_value_usd=Decimal(totals[3]),
        unique_paying_coldkeys=int(totals[4]),
        first_payment_at=totals[5],
        last_payment_at=totals[6],
        address_history=sorted(
            addresses.values(),
            key=lambda row: (not row.is_current, row.payment_address),
        ),
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


def _csv_cell(value: object) -> str:
    text = "" if value is None else str(value)
    # Quote escaping alone does not prevent spreadsheet formula execution.
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) else text


@router.get("/export.csv", response_class=Response)
async def export_miner_fees(_: AdminDep, session: SessionDep) -> Response:
    """Export the complete accepted ledger across all dates and destinations."""
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(
        [
            "timestamp_utc",
            "block_hash",
            "extrinsic_index",
            "destination_address",
            "miner_coldkey",
            "miner_hotkey",
            "agent_id",
            "credit_for_agent_id",
            "amount_rao",
            "amount_tao",
            "tao_usd_rate",
            "historical_value_usd",
            "accepted_under_legacy_fee_amnesty",
        ]
    )
    rows = await session.stream_scalars(
        select(EvaluationPayment)
        .order_by(
            EvaluationPayment.timestamp,
            EvaluationPayment.block_hash,
            EvaluationPayment.extrinsic_index,
        )
        .execution_options(yield_per=1000)
    )
    async for row in rows:
        amount_tao = Decimal(row.amount_rao) / _RAO_PER_TAO
        writer.writerow(
            map(
                _csv_cell,
                [
                    row.timestamp.astimezone(UTC).isoformat(),
                    row.block_hash,
                    row.extrinsic_index,
                    row.dest_address,
                    row.miner_coldkey,
                    row.miner_hotkey,
                    row.agent_id,
                    row.credit_for_agent_id,
                    row.amount_rao,
                    format(amount_tao, ".9f"),
                    row.tao_usd_rate,
                    None if row.tao_usd_rate is None else amount_tao * row.tao_usd_rate,
                    row.accepted_under_legacy_fee_amnesty,
                ],
            )
        )
    return Response(
        output.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition": 'attachment; filename="miner-submission-fees.csv"',
            "Cache-Control": "no-store",
            "Vary": "Authorization",
        },
    )
