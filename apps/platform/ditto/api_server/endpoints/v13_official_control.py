"""Authenticated official-control assets; no miner or verdict authority."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.v13_official_control import (
    OfficialControlApprovalRequest,
    OfficialControlApprovalView,
    OfficialControlImageArtifact,
    OfficialControlImageRequest,
    OfficialControlImageUpload,
    OfficialControlImageVerifyRequest,
    OfficialControlImageView,
)
from ditto.api_server.canonical_starter_control import (
    ARCHIVE_BYTES,
    ARCHIVE_SHA256,
    OBJECT_KEY,
    RELEASE,
    RELEASE_COMMIT,
    SOURCE_TREE,
)
from ditto.api_server.dependencies import get_session, get_storage_client
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.api_server.endpoints.admin_v13_private_generation import _database_now
from ditto.api_server.endpoints.l2_report_canary import _fixture_attestation_valid
from ditto.api_server.operator_proof import require_operator_proof
from ditto.api_server.storage import S3StorageClient
from ditto.db.models import (
    ScreenerL2ReportCanary,
    V13OfficialControlApproval,
    V13OfficialControlImage,
)
from ditto_screening_protocol.v13_private_package import V13_PRIVATE_PROFILE_SHA256

router = APIRouter(prefix="/admin/v13-official-controls", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]
_URL_TTL = 300


def _view(row: V13OfficialControlImage) -> OfficialControlImageView:
    return OfficialControlImageView.model_validate(row, from_attributes=True)


def _approval_view(
    row: V13OfficialControlApproval, image: V13OfficialControlImage
) -> OfficialControlApprovalView:
    return OfficialControlApprovalView(
        **{
            key: getattr(row, key)
            for key in (
                "approval_id",
                "control_id",
                "profile_sha256",
                "review_evidence_sha256",
                "approval_receipt_sha256",
                "actor",
                "reason",
                "approved_at",
            )
        },
        fixture_id=image.fixture_id,
        artifact_sha256=image.artifact_sha256,
        image_sha256=image.image_sha256,
        image_id=image.image_id,
    )


async def _fixture(
    session: AsyncSession,
    fixture_id: UUID,
    *,
    require_review: bool = True,
) -> ScreenerL2ReportCanary:
    row = await session.get(
        ScreenerL2ReportCanary, fixture_id, with_for_update=True, populate_existing=True
    )
    att = row.source_attestation if row is not None else None
    if (
        row is None
        or row.source_kind != "canonical_starter_fixture"
        or row.fixture_key != OBJECT_KEY
        or row.artifact_sha256 != ARCHIVE_SHA256
        or row.policy_version != 13
        or row.bench_version != 13
        or row.run_mode != "source_only"
        or not isinstance(att, dict)
        or any(
            att.get(key) != value
            for key, value in {
                "release": RELEASE,
                "release_commit": RELEASE_COMMIT,
                "source_tree": SOURCE_TREE,
                "archive_sha256": ARCHIVE_SHA256,
                "archive_size_bytes": ARCHIVE_BYTES,
            }.items()
        )
        or not isinstance(att.get("registered_by"), str)
    ):
        raise HTTPException(409, "official fixture source registration is unavailable")
    if require_review and not _fixture_attestation_valid(row):
        raise HTTPException(409, "official fixture is not independently attested")
    return row


def _producer(fixture: ScreenerL2ReportCanary, actor: str, image_id: str) -> None:
    attestation = fixture.source_attestation or {}
    if actor != attestation.get("registered_by"):
        raise HTTPException(403, "only the fixture registrar may stage its image")
    reviewed_id = attestation.get("reviewer_built_image_digest")
    if reviewed_id is not None and image_id != reviewed_id:
        raise HTTPException(
            409, "image identity differs from independently reviewed fixture"
        )


async def _locked_image(
    session: AsyncSession,
    control_id: UUID,
    *,
    require_review: bool = True,
) -> tuple[V13OfficialControlImage, ScreenerL2ReportCanary]:
    # Match staging's lock order: fixture, then image. The fixture identity
    # cannot change even before verification (the database trigger guards it).
    current = await session.get(V13OfficialControlImage, control_id)
    if current is None:
        raise HTTPException(404, "official control not found")
    fixture = await _fixture(session, current.fixture_id, require_review=require_review)
    row = await session.get(
        V13OfficialControlImage,
        control_id,
        with_for_update=True,
        populate_existing=True,
    )
    if row is None:
        raise HTTPException(409, "official control disappeared")
    return row, fixture


def _staging_key(row: V13OfficialControlImage) -> str:
    return f"v13-official-controls/staging/{row.control_id}/{row.staging_id}.tar"


def _metadata(row: V13OfficialControlImage) -> dict[str, str]:
    return {
        "control-id": str(row.control_id),
        "fixture-id": str(row.fixture_id),
        "artifact-sha256": row.artifact_sha256,
        "image-sha256": row.image_sha256,
        "image-id": row.image_id,
    }


@router.get("", response_model=list[OfficialControlImageView])
async def list_official_controls(
    response: Response,
    _admin: AdminDep,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[OfficialControlImageView]:
    response.headers["Cache-Control"] = "no-store"
    rows = (
        await session.scalars(
            select(V13OfficialControlImage)
            .order_by(
                V13OfficialControlImage.created_at.desc(),
                V13OfficialControlImage.control_id,
            )
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return [_view(row) for row in rows]


@router.post("/images", response_model=OfficialControlImageUpload)
async def stage_official_control_image(
    payload: OfficialControlImageRequest,
    request: Request,
    response: Response,
    _admin: AdminDep,
    session: SessionDep,
) -> OfficialControlImageUpload:
    actor = await require_operator_proof(request)
    response.headers["Cache-Control"] = "no-store"
    try:
        async with session.begin():
            fixture = await _fixture(session, payload.fixture_id, require_review=False)
            _producer(fixture, actor, payload.image_id)
            row = await session.scalar(
                select(V13OfficialControlImage)
                .where(V13OfficialControlImage.request_id == payload.request_id)
                .with_for_update()
            )
            if row is None:
                row = V13OfficialControlImage(
                    control_id=uuid4(),
                    request_id=payload.request_id,
                    fixture_id=payload.fixture_id,
                    artifact_sha256=ARCHIVE_SHA256,
                    image_sha256=payload.image_sha256,
                    image_id=payload.image_id,
                    size_bytes=payload.size_bytes,
                    producer=actor,
                    staging_id=uuid4(),
                    created_at=await _database_now(session),
                )
                session.add(row)
                await session.flush()
            elif (
                any(
                    getattr(row, key) != value
                    for key, value in payload.model_dump().items()
                )
                or row.producer != actor
            ):
                raise HTTPException(
                    409, "official control request already pinned differently"
                )
            if row.verified_at is not None:
                raise HTTPException(409, "official control image is already verified")
    except IntegrityError:
        raise HTTPException(
            409, "official control registration raced; reread before retry"
        ) from None
    storage = await get_storage_client(request)
    metadata = _metadata(row)
    url = await storage.presigned_put_url(
        key=_staging_key(row),
        size_bytes=row.size_bytes,
        metadata=metadata,
        expires_in=_URL_TTL,
    )
    return OfficialControlImageUpload(
        control=_view(row),
        upload_url=url,
        required_headers={
            "Content-Length": str(row.size_bytes),
            "Content-Type": "application/x-tar",
            **{f"x-amz-meta-{key}": value for key, value in metadata.items()},
        },
        expires_at=datetime.now(UTC) + timedelta(seconds=_URL_TTL),
    )


@dataclass(frozen=True)
class _ImageSnapshot:
    image_sha256: str
    image_id: str
    size_bytes: int
    staging_key: str
    metadata: dict[str, str]


async def _check_object(
    storage: S3StorageClient, key: str, snapshot: _ImageSnapshot
) -> None:
    head = await storage.head_object(key=key)
    actual = await storage.verify_object_sha256(
        key=key, expected_size_bytes=snapshot.size_bytes
    )
    if (
        head.size_bytes != snapshot.size_bytes
        or head.metadata != snapshot.metadata
        or actual.size_bytes != snapshot.size_bytes
        or actual.sha256 != snapshot.image_sha256
    ):
        raise HTTPException(409, "official control image bytes or metadata changed")


@router.post("/{control_id}/verify", response_model=OfficialControlImageView)
async def verify_official_control_image(
    control_id: UUID,
    payload: OfficialControlImageVerifyRequest,
    request: Request,
    _admin: AdminDep,
    session: SessionDep,
) -> OfficialControlImageView:
    actor = await require_operator_proof(request)
    async with session.begin():
        row, fixture = await _locked_image(session, control_id, require_review=False)
        _producer(fixture, actor, payload.image_id)
        if (
            row.image_id != payload.image_id
            or row.image_sha256 != payload.image_sha256
            or row.producer != actor
        ):
            raise HTTPException(409, "official control image binding changed")
        if row.verified_at is not None:
            return _view(row)
        snapshot = _ImageSnapshot(
            row.image_sha256,
            row.image_id,
            row.size_bytes,
            _staging_key(row),
            _metadata(row),
        )
    # Do not hold database locks while hashing a potentially large archive.
    # Each concurrent verifier copies to its own final key; a loser cannot
    # overwrite the key another verifier already committed.
    final_key = f"v13-official-controls/verified/{control_id}/{uuid4()}.tar"
    storage = await get_storage_client(request)
    try:
        await _check_object(storage, snapshot.staging_key, snapshot)
        await storage.copy_object(source_key=snapshot.staging_key, dest_key=final_key)
        await _check_object(storage, final_key, snapshot)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(
            503, "official control image verification unavailable"
        ) from None
    async with session.begin():
        row, fixture = await _locked_image(session, control_id, require_review=False)
        _producer(fixture, actor, payload.image_id)
        if (
            row.image_id != snapshot.image_id
            or row.image_sha256 != snapshot.image_sha256
            or row.size_bytes != snapshot.size_bytes
            or row.producer != actor
        ):
            raise HTTPException(409, "official control changed during verification")
        if row.verified_at is None:
            row.verified_at = await _database_now(session)
            row.verified_storage_key = final_key
            await session.flush()
        return _view(row)


@router.post("/{control_id}/approvals", response_model=OfficialControlApprovalView)
async def approve_official_control(
    control_id: UUID,
    payload: OfficialControlApprovalRequest,
    request: Request,
    _admin: AdminDep,
    session: SessionDep,
) -> OfficialControlApprovalView:
    actor = await require_operator_proof(request)
    if payload.profile_sha256 != V13_PRIVATE_PROFILE_SHA256:
        raise HTTPException(409, "official control profile mismatch")
    async with session.begin():
        image, fixture = await _locked_image(session, control_id)
        if image.verified_at is None or image.verified_storage_key is None:
            raise HTTPException(409, "official control archive is not verified")
        if image.image_id != (fixture.source_attestation or {}).get(
            "reviewer_built_image_digest"
        ):
            raise HTTPException(
                409, "official image differs from independently reviewed fixture"
            )
        if actor != (fixture.source_attestation or {}).get("reviewed_by"):
            raise HTTPException(403, "authenticated fixture reviewer required")
        if (
            payload.image_id != image.image_id
            or payload.image_sha256 != image.image_sha256
        ):
            raise HTTPException(409, "reviewed official image identity changed")
        row = await session.scalar(
            select(V13OfficialControlApproval).where(
                V13OfficialControlApproval.control_id == control_id,
                V13OfficialControlApproval.profile_sha256 == payload.profile_sha256,
            )
        )
        if row is not None:
            if row.actor != actor or any(
                getattr(row, key) != getattr(payload, key)
                for key in ("profile_sha256", "review_evidence_sha256", "reason")
            ):
                raise HTTPException(
                    409, "official control approval already recorded differently"
                )
            return _approval_view(row, image)
        approved_at = await _database_now(session)
        approval_id = uuid4()
        receipt = {
            "revision": "v13-official-control-approval-v1",
            "approval_id": str(approval_id),
            "control_id": str(control_id),
            "fixture_id": str(image.fixture_id),
            "artifact_sha256": image.artifact_sha256,
            **payload.model_dump(mode="json"),
            "actor": actor,
            "approved_at": approved_at.isoformat(),
        }
        row = V13OfficialControlApproval(
            approval_id=approval_id,
            control_id=control_id,
            profile_sha256=payload.profile_sha256,
            review_evidence_sha256=payload.review_evidence_sha256,
            approval_receipt_sha256=hashlib.sha256(
                json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            actor=actor,
            reason=payload.reason,
            approved_at=approved_at,
        )
        session.add(row)
        await session.flush()
        return _approval_view(row, image)


@router.get("/{control_id}/approvals", response_model=list[OfficialControlApprovalView])
async def list_official_control_approvals(
    control_id: UUID,
    response: Response,
    _admin: AdminDep,
    session: SessionDep,
) -> list[OfficialControlApprovalView]:
    response.headers["Cache-Control"] = "no-store"
    image = await session.get(V13OfficialControlImage, control_id)
    if image is None:
        raise HTTPException(404, "official control not found")
    rows = (
        await session.scalars(
            select(V13OfficialControlApproval)
            .where(V13OfficialControlApproval.control_id == control_id)
            .order_by(
                V13OfficialControlApproval.approved_at,
                V13OfficialControlApproval.approval_id,
            )
        )
    ).all()
    return [_approval_view(row, image) for row in rows]


@router.get("/{control_id}/artifact", response_model=OfficialControlImageArtifact)
async def get_official_control_artifact(
    control_id: UUID,
    request: Request,
    response: Response,
    _admin: AdminDep,
    session: SessionDep,
) -> OfficialControlImageArtifact:
    """Let the independent reviewer inspect sealed bytes before attestation."""
    response.headers["Cache-Control"] = "no-store"
    actor = await require_operator_proof(request)
    image = await session.get(V13OfficialControlImage, control_id)
    if image is None or image.verified_at is None or image.verified_storage_key is None:
        raise HTTPException(409, "official control archive is not verified")
    fixture = await _fixture(session, image.fixture_id, require_review=False)
    attestation = fixture.source_attestation or {}
    reviewer = attestation.get("reviewed_by")
    # A single authenticated operator may register and record independent
    # agent review evidence. Once reviewed, retain that recorded identity.
    if reviewer is not None and actor != reviewer:
        raise HTTPException(403, "authenticated fixture reviewer required")
    storage = await get_storage_client(request)
    snapshot = _ImageSnapshot(
        image.image_sha256,
        image.image_id,
        image.size_bytes,
        image.verified_storage_key,
        _metadata(image),
    )
    try:
        await _check_object(storage, image.verified_storage_key, snapshot)
        url = await storage.presigned_get_url(
            key=image.verified_storage_key,
            expires_in=_URL_TTL,
        )
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, "official control artifact unavailable") from None
    return OfficialControlImageArtifact(
        control=_view(image),
        download_url=url,
        expires_at=datetime.now(UTC) + timedelta(seconds=_URL_TTL),
    )
