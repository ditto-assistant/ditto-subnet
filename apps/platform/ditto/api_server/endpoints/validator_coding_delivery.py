"""Authenticated authoring-only delivery for shadow coding leases."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.coding_artifacts import (
    CodingArtifactDeliveryPhase,
    CodingArtifactKind,
    CodingAuthoringLeaseRequest,
    CodingAuthoringLeaseResponse,
    coding_authoring_lease_signing_message,
)
from ditto.api_models.coding_selection import (
    coding_selection_run_manifest_digest,
)
from ditto.api_server.attestation import verify_signature
from ditto.api_server.coding_artifact_capabilities import (
    CodingArtifactCapabilityIntegrityError,
    CodingArtifactCapabilityUnavailableError,
    project_coding_artifact_capability,
)
from ditto.api_server.dependencies import get_chain_client, get_session
from ditto.api_server.endpoints.validator import (
    ValidatorAuthError,
    _assert_validator_permitted,
)
from ditto.chain import ChainClient
from ditto.coding_selection import (
    CodingSelectionCatalogIntegrityError,
    CodingSelectionCatalogUnavailableError,
)
from ditto.db.queries.coding_task_leases import (
    CodingTaskLeaseIntegrityError,
    CodingTaskLeaseNotAvailableError,
    authorize_coding_shadow_task_delivery,
    build_coding_shadow_task_lease,
)
from ditto.db.queries.validator_auth import (
    ValidatorRequestReplayError,
    consume_validator_nonce,
)

router = APIRouter(prefix="/validator", tags=["validator"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
ChainDep = Annotated[ChainClient, Depends(get_chain_client)]

_REQUEST_MAX_AGE = timedelta(minutes=5)


@router.post(
    "/coding-shadow/authoring-lease",
    response_model=CodingAuthoringLeaseResponse,
    responses={
        401: {"description": "Signature invalid or validator not permitted."},
        404: {"description": "Coding shadow ticket not available to this validator."},
        409: {"description": "Replay, expiry, or immutable authority conflict."},
        503: {"description": "Private catalog or artifact signer unavailable."},
    },
)
async def request_coding_authoring_lease(
    payload: CodingAuthoringLeaseRequest,
    request: Request,
    response: Response,
    chain: ChainDep,
    session: SessionDep,
) -> CodingAuthoringLeaseResponse:
    """Return one task and three authoring capabilities; never grader bytes."""

    response.headers["Cache-Control"] = "no-store"
    signed = coding_authoring_lease_signing_message(
        validator_hotkey=payload.validator_hotkey,
        ticket_id=payload.ticket_id,
        nonce=payload.nonce,
        requested_at=payload.requested_at,
    )
    if not verify_signature(
        signer=payload.validator_hotkey,
        payload=signed,
        signature_hex=payload.signature,
    ):
        raise ValidatorAuthError("coding authoring lease signature did not verify")
    now = datetime.now(UTC)
    requested_at = payload.requested_at.astimezone(UTC)
    if abs(now - requested_at) > _REQUEST_MAX_AGE:
        raise HTTPException(
            status_code=409,
            detail="coding authoring lease request timestamp is stale",
        )
    await _assert_validator_permitted(
        chain,
        request.app.state.config.chain.netuid,
        payload.validator_hotkey,
        network=request.app.state.config.chain.subtensor_network,
    )

    material_source = getattr(request.app.state, "coding_private_catalog_source", None)
    capability_minter = getattr(
        request.app.state, "coding_artifact_capability_minter", None
    )
    if material_source is None or capability_minter is None:
        raise HTTPException(
            status_code=503,
            detail="coding authoring delivery is not configured",
        )

    async with session.begin():
        try:
            await consume_validator_nonce(
                session,
                nonce=payload.nonce,
                validator_hotkey=payload.validator_hotkey,
                now=now,
                expires_at=requested_at + _REQUEST_MAX_AGE,
            )
        except ValidatorRequestReplayError:
            raise HTTPException(
                status_code=409,
                detail="coding authoring lease nonce has already been used",
            ) from None
        try:
            await authorize_coding_shadow_task_delivery(
                session,
                ticket_id=payload.ticket_id,
                validator_hotkey=payload.validator_hotkey,
            )
        except CodingTaskLeaseNotAvailableError:
            raise HTTPException(
                status_code=404,
                detail="coding shadow ticket not available to this validator",
            ) from None

    try:
        lease = await build_coding_shadow_task_lease(
            session,
            ticket_id=payload.ticket_id,
            material_source=material_source,
        )
    except CodingTaskLeaseNotAvailableError:
        raise HTTPException(
            status_code=409,
            detail="coding shadow ticket or certification is no longer active",
        ) from None
    except (
        CodingTaskLeaseIntegrityError,
        CodingSelectionCatalogIntegrityError,
    ):
        raise HTTPException(
            status_code=409,
            detail="coding authoring lease authority is inconsistent",
        ) from None
    except CodingSelectionCatalogUnavailableError:
        raise HTTPException(
            status_code=503,
            detail="coding private catalog is temporarily unavailable",
        ) from None
    finally:
        if session.in_transaction():
            await session.rollback()

    if lease.validator_hotkey != payload.validator_hotkey:
        raise HTTPException(
            status_code=409,
            detail="coding authoring lease validator authority changed",
        )
    try:
        capability_set = await capability_minter.mint_authoring(lease)
        capabilities = [
            project_coding_artifact_capability(
                capability_set,
                kind=kind,
                phase=CodingArtifactDeliveryPhase.AUTHORING,
            )
            for kind in (
                CodingArtifactKind.VISIBLE_BUNDLE,
                CodingArtifactKind.MEMORY_BUNDLE,
                CodingArtifactKind.RESOURCE_PROFILE,
            )
        ]
    except CodingArtifactCapabilityUnavailableError:
        raise HTTPException(
            status_code=503,
            detail="coding artifact signer is temporarily unavailable",
        ) from None
    except CodingArtifactCapabilityIntegrityError:
        raise HTTPException(
            status_code=409,
            detail="coding artifact capability authority is inconsistent",
        ) from None

    selected = lease.task_set_manifest.tasks[0]
    try:
        return CodingAuthoringLeaseResponse(
            schema="dittobench-coding-authoring-lease-v1",
            coding_contract_version=1,
            weight_eligible=False,
            ticket_id=lease.ticket_id,
            ticket_deadline=lease.deadline,
            coding_run_id=lease.run_manifest.coding_run_id,
            run_manifest_sha256=coding_selection_run_manifest_digest(
                lease.run_manifest
            ),
            task_set_manifest_sha256=lease.run_manifest.task_set_manifest_sha256,
            repository_epoch=lease.repository_epoch,
            issue_sha256=selected.issue_sha256,
            runtime_policy_sha256=selected.runtime_policy_sha256,
            budgets_sha256=selected.budgets_sha256,
            issue=lease.issue,
            runtime_policy=lease.runtime_policy,
            budgets=lease.budgets,
            run_manifest=lease.run_manifest,
            capabilities=capabilities,
        )
    except ValidationError:
        raise HTTPException(
            status_code=409,
            detail="coding authoring response authority is inconsistent",
        ) from None
