"""Backroom read of the Season 2 reranker shadow intake (SN-207). Read-only."""

from __future__ import annotations

from typing import Annotated

from dittobench_reranker.contract import CONTRACT, CONTRACT_ID
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.reranker import (
    AdminRerankerSubmission,
    AdminRerankerSubmissionList,
    RerankerSubmissionStatus,
)
from ditto.api_models.upload import _SS58_PATTERN
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.api_server.endpoints.reranker_upload import (
    to_public,
    upload_window,
    uploads_per_window,
)
from ditto.db.models import RerankerSubmission as RerankerSubmissionRow
from ditto.db.queries.reranker import (
    count_reranker_submissions,
    count_reranker_submissions_by_status,
    list_reranker_submissions,
)

router = APIRouter(prefix="/admin/reranker-submissions", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]


def _admin_row(row: RerankerSubmissionRow) -> AdminRerankerSubmission:
    return AdminRerankerSubmission(
        **to_public(row).model_dump(),
        miner_coldkey=row.miner_coldkey,
        upload_sha256=row.upload_sha256,
    )


@router.get("", response_model=AdminRerankerSubmissionList)
async def admin_reranker_submissions(
    _admin: AdminDep,
    session: SessionDep,
    hotkey: Annotated[str | None, Query(pattern=_SS58_PATTERN)] = None,
    status: RerankerSubmissionStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AdminRerankerSubmissionList:
    """Recent reranker submissions with owner identity and status counts.

    ``status_counts`` covers the whole table; ``total`` matches the filters.
    The effective per-owner upload window is reported so an operator can see
    why a miner was refused with 429.
    """
    status_counts = await count_reranker_submissions_by_status(session)
    total = await count_reranker_submissions(
        session, miner_hotkey=hotkey, status=status
    )
    rows = await list_reranker_submissions(
        session, limit=limit, offset=offset, miner_hotkey=hotkey, status=status
    )
    return AdminRerankerSubmissionList(
        contract_id=CONTRACT_ID,
        contract_sha256=CONTRACT.digest(),
        total=total,
        status_counts=status_counts,
        upload_window_seconds=int(upload_window().total_seconds()),
        uploads_per_window=uploads_per_window(),
        limit=limit,
        offset=offset,
        submissions=[_admin_row(row) for row in rows],
    )
