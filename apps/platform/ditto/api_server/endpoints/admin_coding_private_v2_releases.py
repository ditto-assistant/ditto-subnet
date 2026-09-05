"""Admin-only registration and lifecycle audit for private Coding v2 releases."""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime, timedelta
from typing import Annotated

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.exc import IntegrityError as SAIntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.coding_private_v2_registry import (
    AdminCodingPrivateV2ReleaseResponse,
    AdminRegisterCodingPrivateV2ReleaseRequest,
    AdminTransitionCodingPrivateV2ReleaseRequest,
    CodingPrivateV2RegistrationAuthority,
    CodingPrivateV2ReleaseRecord,
)
from ditto.api_server.coding_private_v2_publication import (
    PrivateV2PublicationError,
    private_v2_publication_signing_message,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.db.queries.coding_private_v2_releases import (
    CodingPrivateV2ReleaseConflictError,
    CodingPrivateV2ReleaseInactiveError,
    ReleaseAction,
    append_private_v2_release_event,
    insert_private_v2_release,
    list_private_v2_releases,
)

router = APIRouter(prefix="/admin/coding-private-v2-releases", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]
_MAX_CLOCK_SKEW = timedelta(minutes=5)


def _register_confirmation(
    registration: CodingPrivateV2RegistrationAuthority,
    curator_signing_key_sha256: str,
) -> str:
    return (
        "REGISTER SHADOW CODING PRIVATE V2 RELEASE "
        f"{registration.corpus_release_id} {registration.registration_sha256} "
        f"{curator_signing_key_sha256}"
    )


def _transition_confirmation(
    *, action: ReleaseAction, corpus_release_id: str, registration_sha256: str
) -> str:
    verb = "QUARANTINE" if action == "quarantined" else "RETIRE"
    return (
        f"{verb} SHADOW CODING PRIVATE V2 RELEASE "
        f"{corpus_release_id} {registration_sha256}"
    )


def _validate_registration_authority(
    payload: AdminRegisterCodingPrivateV2ReleaseRequest,
) -> None:
    registration = payload.registration
    receipt = payload.publication_receipt
    if registration.previous_registration_sha256 is not None:
        raise HTTPException(
            status_code=409,
            detail="private v2 supersession is not enabled",
        )
    linked = (
        registration.publication_receipt_sha256 == receipt.receipt_payload_sha256
        and registration.catalog_sha256 == receipt.catalog_sha256
        and registration.catalog_merkle_root == receipt.catalog_merkle_root
        and registration.payload_sha256 == receipt.payload_sha256
        and registration.transport_sha256 == receipt.transport_sha256
        and registration.wrapping_key_sha256 == receipt.wrapping_key_sha256
    )
    if not linked:
        raise HTTPException(
            status_code=409,
            detail="private v2 registration and publication authorities differ",
        )
    receipt_time = datetime.fromisoformat(
        receipt.checked_at.replace("Z", "+00:00")
    ).astimezone(UTC)
    if receipt_time > datetime.now(UTC) + _MAX_CLOCK_SKEW:
        raise HTTPException(
            status_code=409,
            detail="private v2 publication receipt is too far in the future",
        )
    try:
        public_key = serialization.load_pem_public_key(
            payload.curator_public_key_pem.encode("utf-8")
        )
    except (TypeError, ValueError) as error:
        raise HTTPException(
            status_code=422,
            detail="private v2 curator public key is invalid",
        ) from error
    if not isinstance(public_key, Ed25519PublicKey):
        raise HTTPException(
            status_code=422,
            detail="private v2 curator public key must be Ed25519",
        )
    raw_public_key = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    if hashlib.sha256(raw_public_key).hexdigest() != receipt.curator_signing_key_sha256:
        raise HTTPException(
            status_code=403,
            detail="private v2 curator public key identity differs from receipt",
        )
    try:
        signature = base64.b64decode(receipt.curator_signature_b64, validate=True)
        message = private_v2_publication_signing_message(
            manifest={
                "catalog_merkle_root": receipt.catalog_merkle_root,
                "catalog_sha256": receipt.catalog_sha256,
                "coding_contract_version": 2,
                "objects": receipt.objects,
                "payload_sha256": receipt.payload_sha256,
                "schema": "dittobench-coding-private-v2-transport-v1",
                "transport_sha256": receipt.transport_sha256,
                "weight_eligible": False,
                "wrapping_key_sha256": receipt.wrapping_key_sha256,
            },
            source_sha=receipt.source_sha,
            probe_receipt_payload_sha256=(receipt.probe_receipt_payload_sha256),
            private_input_authority_sha256=(receipt.private_input_authority_sha256),
            curator_signing_key_sha256=receipt.curator_signing_key_sha256,
        )
        public_key.verify(signature, message)
    except (InvalidSignature, PrivateV2PublicationError) as error:
        raise HTTPException(
            status_code=401,
            detail="private v2 curator signature did not verify",
        ) from error


async def _response(
    session: AsyncSession, *, limit: int
) -> AdminCodingPrivateV2ReleaseResponse:
    bundles, total = await list_private_v2_releases(session, limit=limit)
    releases = []
    for bundle in bundles:
        latest = bundle.latest_event
        releases.append(
            CodingPrivateV2ReleaseRecord(
                release_row_id=bundle.release.release_row_id,
                registration=CodingPrivateV2RegistrationAuthority.model_validate(
                    bundle.release.registration_authority
                ),
                publication_source_sha=bundle.release.publication_source_sha,
                provider_probe_receipt_sha256=(
                    bundle.release.provider_probe_receipt_sha256
                ),
                private_input_authority_sha256=(
                    bundle.release.private_input_authority_sha256
                ),
                curator_signing_key_sha256=(bundle.release.curator_signing_key_sha256),
                publication_object_count=bundle.release.publication_object_count,
                status=bundle.status,
                registered_reason=bundle.release.reason,
                registered_actor=bundle.release.actor,
                registered_at=bundle.release.created_at,
                lifecycle_event_count=len(bundle.events),
                latest_event_reason=latest.reason if latest is not None else None,
                latest_event_actor=latest.actor if latest is not None else None,
                latest_event_at=latest.created_at if latest is not None else None,
                shadow_only=True,
                selectable=False,
                weight_eligible=False,
            )
        )
    return AdminCodingPrivateV2ReleaseResponse(
        total=total,
        releases=releases,
        shadow_only=True,
        selectable=False,
        weight_eligible=False,
    )


@router.get("", response_model=AdminCodingPrivateV2ReleaseResponse)
async def get_private_v2_releases(
    response: Response,
    _admin: AdminDep,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> AdminCodingPrivateV2ReleaseResponse:
    response.headers["Cache-Control"] = "no-store"
    return await _response(session, limit=limit)


@router.post("/register", response_model=AdminCodingPrivateV2ReleaseResponse)
async def register_private_v2_release(
    payload: AdminRegisterCodingPrivateV2ReleaseRequest,
    response: Response,
    _admin: AdminDep,
    session: SessionDep,
) -> AdminCodingPrivateV2ReleaseResponse:
    response.headers["Cache-Control"] = "no-store"
    expected = _register_confirmation(
        payload.registration,
        payload.publication_receipt.curator_signing_key_sha256,
    )
    if payload.confirmation != expected:
        raise HTTPException(
            status_code=422,
            detail=f'confirmation must equal "{expected}"',
        )
    _validate_registration_authority(payload)
    try:
        async with session.begin():
            await insert_private_v2_release(
                session,
                registration=payload.registration,
                receipt=payload.publication_receipt,
                reason=payload.reason,
                actor=payload.actor,
            )
    except CodingPrivateV2ReleaseConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except SAIntegrityError as error:
        raise HTTPException(
            status_code=409,
            detail="private v2 registration changed concurrently",
        ) from error
    return await _response(session, limit=50)


async def _transition(
    *,
    payload: AdminTransitionCodingPrivateV2ReleaseRequest,
    action: ReleaseAction,
    response: Response,
    session: AsyncSession,
) -> AdminCodingPrivateV2ReleaseResponse:
    response.headers["Cache-Control"] = "no-store"
    expected = _transition_confirmation(
        action=action,
        corpus_release_id=payload.corpus_release_id,
        registration_sha256=payload.expected_registration_sha256,
    )
    if payload.confirmation != expected:
        raise HTTPException(
            status_code=422,
            detail=f'confirmation must equal "{expected}"',
        )
    try:
        async with session.begin():
            await append_private_v2_release_event(
                session,
                corpus_release_id=payload.corpus_release_id,
                expected_registration_sha256=(payload.expected_registration_sha256),
                action=action,
                reason=payload.reason,
                actor=payload.actor,
            )
    except CodingPrivateV2ReleaseConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except CodingPrivateV2ReleaseInactiveError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except SAIntegrityError as error:
        raise HTTPException(
            status_code=409,
            detail="private v2 lifecycle transition changed concurrently",
        ) from error
    return await _response(session, limit=50)


@router.post("/quarantine", response_model=AdminCodingPrivateV2ReleaseResponse)
async def quarantine_private_v2_release(
    payload: AdminTransitionCodingPrivateV2ReleaseRequest,
    response: Response,
    _admin: AdminDep,
    session: SessionDep,
) -> AdminCodingPrivateV2ReleaseResponse:
    return await _transition(
        payload=payload,
        action="quarantined",
        response=response,
        session=session,
    )


@router.post("/retire", response_model=AdminCodingPrivateV2ReleaseResponse)
async def retire_private_v2_release(
    payload: AdminTransitionCodingPrivateV2ReleaseRequest,
    response: Response,
    _admin: AdminDep,
    session: SessionDep,
) -> AdminCodingPrivateV2ReleaseResponse:
    return await _transition(
        payload=payload,
        action="retired",
        response=response,
        session=session,
    )
