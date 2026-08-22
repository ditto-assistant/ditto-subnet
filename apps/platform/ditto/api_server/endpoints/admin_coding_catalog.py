"""Admin control and redacted visibility for private coding catalogs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy.exc import IntegrityError as SAIntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.coding_catalog import (
    AdminCodingCatalogResponse,
    AdminRegisterCodingCatalogRequest,
    AdminRetireCodingCatalogRequest,
    CodingCatalogCommitment,
    CodingCatalogReleaseRecord,
    coding_catalog_commitment_signing_message,
)
from ditto.api_server.attestation import verify_signature
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.db.queries.coding_catalog import (
    CodingCatalogConflictError,
    CodingCatalogInactiveError,
    insert_coding_catalog_release,
    list_coding_catalog_releases,
    retire_coding_catalog_release,
)

router = APIRouter(prefix="/admin/coding-catalog", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]
_MAX_COMMITMENT_CLOCK_SKEW = timedelta(minutes=5)


def _register_confirmation(corpus_release_id: str) -> str:
    return f"REGISTER SHADOW CODING CATALOG {corpus_release_id}"


def _retire_confirmation(corpus_release_id: str) -> str:
    return f"RETIRE SHADOW CODING CATALOG {corpus_release_id}"


async def _response(
    session: AsyncSession,
    *,
    limit: int,
) -> AdminCodingCatalogResponse:
    bundles, total = await list_coding_catalog_releases(session, limit=limit)
    releases = [
        CodingCatalogReleaseRecord(
            release_row_id=bundle.release.release_row_id,
            commitment=CodingCatalogCommitment.model_validate(
                bundle.release.commitment
            ),
            signature=bundle.release.signature,
            registered_reason=bundle.release.reason,
            registered_actor=bundle.release.actor,
            registered_at=bundle.release.created_at,
            retired=bundle.retirement is not None,
            retired_reason=(
                bundle.retirement.reason if bundle.retirement is not None else None
            ),
            retired_actor=(
                bundle.retirement.actor if bundle.retirement is not None else None
            ),
            retired_at=(
                bundle.retirement.retired_at if bundle.retirement is not None else None
            ),
            exposure_count=bundle.exposure_count,
            exposed_run_count=bundle.exposed_run_count,
            shadow_only=True,
        )
        for bundle in bundles
    ]
    return AdminCodingCatalogResponse(
        total=total,
        releases=releases,
        shadow_only=True,
    )


@router.get("/releases", response_model=AdminCodingCatalogResponse)
async def get_coding_catalog_releases(
    response: Response,
    _admin: AdminDep,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> AdminCodingCatalogResponse:
    response.headers["Cache-Control"] = "no-store"
    return await _response(session, limit=limit)


@router.post("/releases", response_model=AdminCodingCatalogResponse)
async def register_coding_catalog_release(
    payload: AdminRegisterCodingCatalogRequest,
    request: Request,
    response: Response,
    _admin: AdminDep,
    session: SessionDep,
) -> AdminCodingCatalogResponse:
    response.headers["Cache-Control"] = "no-store"
    commitment = payload.commitment
    expected_confirmation = _register_confirmation(commitment.corpus_release_id)
    if payload.confirmation != expected_confirmation:
        raise HTTPException(
            status_code=422,
            detail=f'confirmation must equal "{expected_confirmation}"',
        )
    if datetime.fromtimestamp(commitment.committed_at_unix, UTC) > (
        datetime.now(UTC) + _MAX_COMMITMENT_CLOCK_SKEW
    ):
        raise HTTPException(
            status_code=409,
            detail="coding catalog commitment is too far in the future",
        )
    configured_hotkeys = request.app.state.config.coding_catalog_curator_hotkeys
    if commitment.curator_hotkey not in configured_hotkeys:
        raise HTTPException(
            status_code=403,
            detail=(
                "coding catalog curator is not configured"
                if configured_hotkeys
                else "coding catalog registration is disabled"
            ),
        )
    if not verify_signature(
        signer=commitment.curator_hotkey,
        payload=coding_catalog_commitment_signing_message(commitment),
        signature_hex=payload.signature,
    ):
        raise HTTPException(
            status_code=401,
            detail="coding catalog curator signature did not verify",
        )
    try:
        async with session.begin():
            await insert_coding_catalog_release(
                session,
                commitment=commitment,
                signature=payload.signature,
                reason=payload.reason,
                actor=payload.actor,
            )
    except CodingCatalogConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except SAIntegrityError as error:
        raise HTTPException(
            status_code=409,
            detail="coding catalog registration changed concurrently",
        ) from error
    return await _response(session, limit=50)


@router.post("/retire", response_model=AdminCodingCatalogResponse)
async def retire_coding_catalog(
    payload: AdminRetireCodingCatalogRequest,
    response: Response,
    _admin: AdminDep,
    session: SessionDep,
) -> AdminCodingCatalogResponse:
    response.headers["Cache-Control"] = "no-store"
    expected_confirmation = _retire_confirmation(payload.corpus_release_id)
    if payload.confirmation != expected_confirmation:
        raise HTTPException(
            status_code=422,
            detail=f'confirmation must equal "{expected_confirmation}"',
        )
    try:
        async with session.begin():
            await retire_coding_catalog_release(
                session,
                corpus_release_id=payload.corpus_release_id,
                expected_commitment_sha256=payload.expected_commitment_sha256,
                reason=payload.reason,
                actor=payload.actor,
            )
    except CodingCatalogConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except CodingCatalogInactiveError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except SAIntegrityError as error:
        raise HTTPException(
            status_code=409,
            detail="coding catalog retirement changed concurrently",
        ) from error
    return await _response(session, limit=50)
