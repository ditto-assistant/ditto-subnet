"""Authenticated official-control assets; no miner or verdict authority."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.v13_official_control import (
    OfficialControlApprovalView,
    OfficialControlImageArtifact,
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
from ditto.api_server.endpoints.l2_report_canary import _fixture_attestation_valid
from ditto.api_server.operator_proof import require_operator_proof
from ditto.api_server.storage import S3StorageClient
from ditto.db.models import (
    ScreenerL2ReportCanary,
    V13OfficialControlApproval,
    V13OfficialControlImage,
)

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
