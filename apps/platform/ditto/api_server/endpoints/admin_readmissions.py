"""Operator read of every SN-485 readmission (Backroom ``list_readmissions``).

Read-only. See :mod:`ditto.api_models.admin_readmissions`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.admin_readmissions import (
    AdminReadmissionsResponse,
    PaidReadmissionRow,
    ReadmissionTotals,
    UploadReadmissionRow,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.api_server.readmission import activation_state, cost_basis_view
from ditto.api_server.readmission_cost_basis import readmission_cost_basis
from ditto.db.models import (
    Agent,
    BenchmarkRolloutAudit,
    EvaluationPayment,
    ReadmissionPayment,
    UploadReadmission,
)
from ditto.db.queries.benchmark_admission import activated_rollout_for_version
from ditto.db.queries.payments import credit_expires_at
from ditto.db.queries.readmission import readmission_fee_totals
from ditto.db.queries.readmission_gate import current_readmission_activation
from ditto.db.queries.stranded_admission import EVENT_STRANDED_ADMITTED
from ditto.db.queries.submission_settings import effective_submission_settings

router = APIRouter(prefix="/admin/readmissions", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]


@router.get("", response_model=AdminReadmissionsResponse)
async def list_readmissions(
    request: Request,
    _admin: AdminDep,
    session: SessionDep,
    bench_version: Annotated[int | None, Query(ge=1)] = None,
    miner_coldkey: Annotated[str | None, Query(max_length=128)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> AdminReadmissionsResponse:
    """Every paid, credited and classified readmission, newest first."""
    generated_at = datetime.now(UTC)
    settings = await effective_submission_settings(
        session, default_payment_address=request.app.state.config.upload_payment_address
    )
    activation = await current_readmission_activation(session, settings=settings)
    basis = await readmission_cost_basis(
        session, bench_version=activation.active_bench_version
    )

    paid_stmt = select(ReadmissionPayment, Agent.name).outerjoin(
        Agent, Agent.agent_id == ReadmissionPayment.agent_id
    )
    upload_stmt = (
        select(UploadReadmission, Agent.name, EvaluationPayment.amount_rao)
        .outerjoin(Agent, Agent.agent_id == UploadReadmission.agent_id)
        .outerjoin(
            EvaluationPayment, EvaluationPayment.agent_id == UploadReadmission.agent_id
        )
    )
    if bench_version is not None:
        paid_stmt = paid_stmt.where(ReadmissionPayment.bench_version == bench_version)
        upload_stmt = upload_stmt.where(
            UploadReadmission.bench_version == bench_version
        )
    if miner_coldkey is not None:
        paid_stmt = paid_stmt.where(ReadmissionPayment.miner_coldkey == miner_coldkey)
        upload_stmt = upload_stmt.where(
            UploadReadmission.miner_coldkey == miner_coldkey
        )
    paid_rows = (
        await session.execute(
            paid_stmt.order_by(ReadmissionPayment.updated_at.desc())
            .offset(offset)
            .limit(limit + 1)
        )
    ).all()
    upload_rows = (
        await session.execute(
            upload_stmt.order_by(UploadReadmission.created_at.desc())
            .offset(offset)
            .limit(limit + 1)
        )
    ).all()
    truncated = len(paid_rows) > limit or len(upload_rows) > limit

    paid: list[PaidReadmissionRow] = []
    for row, name in paid_rows[:limit]:
        credit_spent = None
        expires = None
        if row.outcome == "credited":
            credit = await session.scalar(
                select(EvaluationPayment).where(
                    EvaluationPayment.block_hash == row.block_hash,
                    EvaluationPayment.extrinsic_index == row.extrinsic_index,
                )
            )
            if credit is not None:
                credit_spent = credit.agent_id is not None
                expires = None if credit_spent else credit_expires_at(credit)
        paid.append(
            PaidReadmissionRow(
                block_hash=row.block_hash,
                extrinsic_index=row.extrinsic_index,
                agent_id=row.agent_id,
                agent_name=name,
                miner_hotkey=row.miner_hotkey,
                miner_coldkey=row.miner_coldkey,
                bench_version=row.bench_version,
                rollout_id=row.rollout_id,
                outcome="admitted" if row.outcome == "admitted" else "credited",
                funding="credit" if row.funding == "credit" else "fresh",
                amount_rao=row.amount_rao,
                refusal_reason=row.refusal_reason,
                credit_audit=row.credit_audit,
                credit_spent=credit_spent,
                credit_expires_at=expires,
                recorded_at=row.created_at,
                updated_at=row.updated_at,
            )
        )
    uploads = [
        UploadReadmissionRow(
            agent_id=row.agent_id,
            agent_name=name,
            kind="identical_rescore"
            if row.kind == "identical_rescore"
            else ("near_duplicate"),
            lineage_agent_id=row.lineage_agent_id,
            lineage_evaluation_completed=row.lineage_evaluation_completed,
            bench_version=row.bench_version,
            miner_coldkey=row.miner_coldkey,
            amount_rao=amount,
            created_at=row.created_at,
        )
        for row, name, amount in upload_rows[:limit]
    ]

    totals = await readmission_fee_totals(session, bench_version=bench_version)
    categories: dict[str, int] = {}
    rollout = await activated_rollout_for_version(
        session, bench_version=activation.active_bench_version
    )
    if rollout is not None:
        category = func.coalesce(
            BenchmarkRolloutAudit.payload["category"].as_string(), "unspecified"
        )
        for name, count in (
            await session.execute(
                select(category, func.count())
                .where(
                    BenchmarkRolloutAudit.rollout_id == rollout.rollout_id,
                    BenchmarkRolloutAudit.event == EVENT_STRANDED_ADMITTED,
                )
                .group_by(category)
            )
        ).tuples():
            categories[str(name)] = int(count)

    return AdminReadmissionsResponse(
        activation=activation_state(activation),
        cost_basis=cost_basis_view(basis),
        bench_version=bench_version,
        totals=ReadmissionTotals(
            admitted=totals.admitted,
            admitted_amount_rao=totals.admitted_amount_rao,
            credit_funded=totals.credit_funded,
            credited=totals.credited,
            credited_amount_rao=totals.credited_amount_rao,
            upload_readmissions=totals.upload_readmissions,
            upload_readmission_amount_rao=totals.upload_readmission_amount_rao,
        ),
        free_admissions_by_category=categories,
        paid=paid,
        uploads=uploads,
        truncated=truncated,
        generated_at=generated_at,
    )
