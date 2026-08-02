"""Audited operator control for public source-release timing."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.artifact_release_settings import (
    AdminArtifactReleaseSettingsRequest,
    AdminArtifactReleaseSettingsResponse,
)
from ditto.api_models.artifact_release_settings import (
    ArtifactReleaseSettingsRevision as RevisionModel,
)
from ditto.api_models.source_disclosure import SourceDisclosure, release_confirmation
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.db.models import ArtifactReleaseSettingsRevision
from ditto.db.queries.artifact_release_settings import (
    DEFAULT_ARTIFACT_RELEASE_DISCLOSURE,
    DEFAULT_ARTIFACT_RELEASE_EMBARGO_HOURS,
    latest_artifact_release_settings,
)

router = APIRouter(prefix="/admin/artifact-release-settings", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]


def _revision(row: ArtifactReleaseSettingsRevision) -> RevisionModel:
    return RevisionModel(
        revision=row.revision,
        parent_revision=row.parent_revision,
        disclosure=SourceDisclosure(row.disclosure),
        embargo_hours=row.embargo_hours,
        reason=row.reason,
        actor=row.actor,
        created_at=row.created_at,
    )


def _default_revision() -> RevisionModel:
    return RevisionModel(
        revision=0,
        parent_revision=0,
        disclosure=DEFAULT_ARTIFACT_RELEASE_DISCLOSURE,
        embargo_hours=DEFAULT_ARTIFACT_RELEASE_EMBARGO_HOURS,
        reason="Built-in privacy-first default",
        actor="platform",
        created_at=None,
    )


@router.get("", response_model=AdminArtifactReleaseSettingsResponse)
async def get_settings(
    _admin: AdminDep,
    session: SessionDep,
) -> AdminArtifactReleaseSettingsResponse:
    rows = list(
        await session.scalars(
            select(ArtifactReleaseSettingsRevision)
            .order_by(ArtifactReleaseSettingsRevision.revision.desc())
            .limit(100)
        )
    )
    return AdminArtifactReleaseSettingsResponse(
        current=_revision(rows[0]) if rows else _default_revision(),
        history=[_revision(row) for row in rows],
    )


@router.post("", response_model=RevisionModel)
async def create_settings_revision(
    payload: AdminArtifactReleaseSettingsRequest,
    _admin: AdminDep,
    session: SessionDep,
) -> RevisionModel:
    """Set the global release policy with CAS and an append-only audit record.

    Two values on one revision. ``disclosure`` says whether source is ever
    published; ``embargo_hours`` says how soon, anywhere in the 6-hour-to-one-
    year range. 48 hours remains the community-agreed default, not a cap.

    They are written together because they are one decision. Split across two
    endpoints, a policy change from "48 hours" to "never" would be two writes
    with a window between them in which the subnet was in neither state, and
    the audit would record a transition nobody chose.

    ``embargo_hours`` stays required and in range even when disclosure is
    ``never``: it is retained so that returning to ``public`` restores the
    window the subnet last agreed on, rather than forcing one to be re-chosen
    under whatever pressure prompted the reversal.

    Shortening a window still releases source earlier and cannot be reversed,
    so the console surfaces that warning; the server only enforces the CAS
    revision and the exact confirmation phrase.
    """
    expected_confirmation = release_confirmation(
        payload.disclosure, payload.embargo_hours
    )
    if payload.confirmation != expected_confirmation:
        raise HTTPException(
            status_code=409,
            detail=f"confirmation must be exactly {expected_confirmation}",
        )

    latest = await latest_artifact_release_settings(session)
    actual_revision = latest.revision if latest is not None else 0
    if payload.expected_revision != actual_revision:
        raise HTTPException(
            status_code=409,
            detail=(
                "artifact release settings changed; refresh before applying "
                f"(expected {payload.expected_revision}, current {actual_revision})"
            ),
        )

    row = ArtifactReleaseSettingsRevision(
        parent_revision=actual_revision,
        disclosure=payload.disclosure.value,
        embargo_hours=payload.embargo_hours,
        reason=payload.reason.strip(),
        actor=payload.actor.strip(),
    )
    session.add(row)
    try:
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail=(
                "artifact release settings changed concurrently; "
                "refresh before applying"
            ),
        ) from error
    await session.refresh(row)
    return _revision(row)
