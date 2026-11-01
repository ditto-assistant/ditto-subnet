"""Operator read: provider-outage circuits and the post-outage sweep's runs."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.provider_outages import (
    OutageSweepPolicy,
    OutageSweepRunView,
    ProviderOutageCircuitView,
    ProviderOutagesResponse,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.db.models import ProviderOutageCircuit
from ditto.db.queries.confirmation_score_voids import (
    MIN_OUTAGE_FAILED_CALLS,
    OUTAGE_FAILED_CALLS,
    OUTAGE_FAILED_SHARE,
    OUTAGE_FAILURE_CODES,
)
from ditto.db.queries.outage_sweep import (
    SWEEP_BATCH_LIMIT,
    SWEEP_FIRST_LOOKBACK,
    SWEEP_INTERVAL_SECONDS,
    SWEEP_SETTLE_LAG,
    list_outage_sweep_runs,
)
from ditto.db.queries.provider_outages import provider_outage_active

router = APIRouter(prefix="/admin", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]

_POLICY = OutageSweepPolicy(
    interval_seconds=SWEEP_INTERVAL_SECONDS,
    batch_limit=SWEEP_BATCH_LIMIT,
    first_lookback_seconds=SWEEP_FIRST_LOOKBACK.total_seconds(),
    settle_lag_seconds=SWEEP_SETTLE_LAG.total_seconds(),
    outage_failure_codes=sorted(OUTAGE_FAILURE_CODES),
    min_outage_failed_calls=MIN_OUTAGE_FAILED_CALLS,
    outage_failed_calls=OUTAGE_FAILED_CALLS,
    outage_failed_share=OUTAGE_FAILED_SHARE,
)


@router.get("/provider-outages", response_model=ProviderOutagesResponse)
async def get_provider_outages(
    _admin: AdminDep,
    session: SessionDep,
    with_findings: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ProviderOutagesResponse:
    """Current circuit rows plus newest-first post-outage sweep runs.

    ``with_findings`` keeps only runs that voided, flagged or errored. Every
    list carries ids, counts and timestamps only.
    """
    now = datetime.now(UTC)
    circuits = list(
        await session.scalars(
            select(ProviderOutageCircuit).order_by(ProviderOutageCircuit.provider)
        )
    )
    total, runs = await list_outage_sweep_runs(
        session, with_findings=with_findings, limit=limit, offset=offset
    )
    return ProviderOutagesResponse(
        circuits=[
            ProviderOutageCircuitView(
                provider=circuit.provider,
                state=circuit.state,  # type: ignore[arg-type]
                epoch=circuit.epoch,
                opened_at=circuit.opened_at,
                retry_at=circuit.retry_at,
                last_failure_at=circuit.last_failure_at,
                closed_at=circuit.closed_at,
                failure_count=circuit.failure_count,
                last_status=circuit.last_status,
                last_error_code=circuit.last_error_code,
                probe_kind=circuit.probe_kind,
                probe_expires_at=circuit.probe_expires_at,
                updated_at=circuit.updated_at,
                outage_active=provider_outage_active(circuit, now=now),
            )
            for circuit in circuits
        ],
        sweep_policy=_POLICY,
        runs_total=total,
        limit=limit,
        offset=offset,
        runs=[
            OutageSweepRunView.model_validate(
                {
                    "sweep_id": run.sweep_id,
                    "bench_version": run.bench_version,
                    "started_at": run.started_at,
                    "finished_at": run.finished_at,
                    "watermark_from": run.watermark_from,
                    "watermark_to": run.watermark_to,
                    "examined_count": run.examined_count,
                    "voided_count": run.voided_count,
                    "error_count": run.error_count,
                    "voids": run.voids,
                    "outage_windows": run.outage_windows,
                    "canonical_flagged": run.canonical_flagged,
                    "errors": run.errors,
                }
            )
            for run in runs
        ],
    )
