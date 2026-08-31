"""Default-off operator entry point for one shadow coding reconciliation."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.exc import IntegrityError as SAIntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.coding_evaluation import (
    AdminCodingShadowReconciliationRequest,
    AdminCodingShadowReconciliationResponse,
)
from ditto.api_server.dependencies import get_chain_client, get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.chain import ChainClient
from ditto.coding_selection import (
    CodingSelectionCatalogUnavailableError,
    CodingSelectionChainUnavailableError,
    CodingSelectionError,
)
from ditto.db.queries.coding_assignments import (
    CodingAssignmentConflictError,
    CodingAssignmentNotQualifiedError,
)
from ditto.db.queries.coding_issuance import (
    CodingIssuanceConflictError,
    CodingIssuanceIntegrityError,
    CodingIssuanceNotQualifiedError,
    CodingIssuanceUnavailableError,
)
from ditto.db.queries.coding_reconciliation import (
    CodingReconciliationPolicy,
    reconcile_shadow_coding_run,
)

router = APIRouter(prefix="/admin/coding-shadow", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
ChainDep = Annotated[ChainClient, Depends(get_chain_client)]
AdminDep = Annotated[None, Depends(require_admin)]


def _confirmation(payload: AdminCodingShadowReconciliationRequest) -> str:
    return (
        "RECONCILE SHADOW CODING "
        f"{payload.agent_id} {payload.bench_version} "
        f"{payload.corpus_release_id} {payload.coding_run_id}"
    )


@router.post(
    "/reconcile",
    response_model=AdminCodingShadowReconciliationResponse,
    responses={
        401: {"description": "Admin authentication failed."},
        409: {"description": "The named artifact or its authority is unavailable."},
        503: {"description": "Shadow reconciliation is disabled or unavailable."},
    },
)
async def reconcile_coding_shadow_artifact(
    payload: AdminCodingShadowReconciliationRequest,
    request: Request,
    response: Response,
    _admin: AdminDep,
    chain: ChainDep,
    session: SessionDep,
) -> AdminCodingShadowReconciliationResponse:
    """Advance one named artifact without scheduling or issuing tickets."""

    response.headers["Cache-Control"] = "no-store"
    config = request.app.state.config
    if not config.coding_shadow_reconciliation_enabled:
        raise HTTPException(
            status_code=503,
            detail="coding shadow reconciliation is disabled",
        )
    expected_confirmation = _confirmation(payload)
    if payload.confirmation != expected_confirmation:
        raise HTTPException(
            status_code=422,
            detail=f'confirmation must equal "{expected_confirmation}"',
        )
    catalog_source = getattr(request.app.state, "coding_private_catalog_source", None)
    if catalog_source is None:
        raise HTTPException(
            status_code=503,
            detail="coding private catalog is unavailable",
        )
    try:
        async with session.begin():
            result = await reconcile_shadow_coding_run(
                session,
                finalized_source=chain,
                catalog_source=catalog_source,
                agent_id=payload.agent_id,
                bench_version=payload.bench_version,
                coding_run_id=payload.coding_run_id,
                corpus_release_id=payload.corpus_release_id,
                policy=CodingReconciliationPolicy(
                    selection_delay_blocks=(
                        config.coding_shadow_reconciliation_selection_delay_blocks
                    ),
                ),
            )
    except (
        CodingIssuanceUnavailableError,
        CodingSelectionCatalogUnavailableError,
        CodingSelectionChainUnavailableError,
    ) as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except (
        CodingAssignmentConflictError,
        CodingAssignmentNotQualifiedError,
        CodingIssuanceConflictError,
        CodingIssuanceIntegrityError,
        CodingIssuanceNotQualifiedError,
        CodingSelectionError,
        SAIntegrityError,
    ) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return AdminCodingShadowReconciliationResponse(
        state=result.state.value,
        assignment_row_id=result.assignment_row_id,
        selection_block_number=result.selection_block_number,
        run_row_id=result.run_row_id,
        assignment_idempotent=result.assignment_idempotent,
        issuance_idempotent=result.issuance_idempotent,
    )
