"""Audited control for the Ditto Edge shadow lane (SN-481).

Mirrors ``admin_burn_settings``: optimistic concurrency on
``expected_revision``, a typed confirmation phrase, and an append-only revision
storing the whole policy. The lane is ``off`` until a revision says
``shadow``. No value of this setting reaches weights or emissions: the only
modes are ``off`` and ``shadow``.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.edge_lane import (
    CONFIRMATION,
    AdminEdgeLaneSettingsRequest,
    AdminEdgeLaneSettingsResponse,
    EdgeLaneSettings,
    EdgeLaneSettingsRevision,
    EffectiveEdgeLaneSettings,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.edge_lane_settings import (
    DEFAULT_SETTINGS,
    EdgeLaneSettingsResolver,
    settings_from_row,
)
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.db.models import EdgeLaneSettingsRevision as RevisionRow
from ditto.db.queries.edge_lane_settings import (
    GLOBAL_SCOPE,
    insert_edge_lane_settings_revision,
    latest_edge_lane_settings_revision,
    list_edge_lane_settings_revisions,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/edge-lane-settings", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]


def _checksum(settings: EdgeLaneSettings) -> str:
    encoded = json.dumps(
        settings.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _revision(row: RevisionRow) -> EdgeLaneSettingsRevision:
    return EdgeLaneSettingsRevision(
        revision=row.revision,
        parent_revision=row.parent_revision,
        scope=row.scope,
        settings=settings_from_row(row),
        reason=row.reason,
        actor=row.actor,
        created_at=row.created_at,
        checksum=row.checksum,
    )


def _resolver(request: Request) -> EdgeLaneSettingsResolver:
    resolver = getattr(request.app.state, "edge_lane_settings", None)
    if resolver is None:  # pragma: no cover
        raise HTTPException(status_code=503, detail="edge lane settings unavailable")
    return resolver


@router.get("", response_model=AdminEdgeLaneSettingsResponse)
async def get_settings(
    request: Request, _admin: AdminDep, session: SessionDep
) -> AdminEdgeLaneSettingsResponse:
    resolver = _resolver(request)
    latest = await latest_edge_lane_settings_revision(session)
    history = await list_edge_lane_settings_revisions(session)
    return AdminEdgeLaneSettingsResponse(
        current=[_revision(latest)] if latest is not None else [],
        history=[_revision(row) for row in history],
        default=DEFAULT_SETTINGS,
        effective=EffectiveEdgeLaneSettings(
            revision=latest.revision if latest is not None else 0,
            scope=latest.scope if latest is not None else GLOBAL_SCOPE,
            settings=settings_from_row(latest),
            checksum=latest.checksum if latest is not None else "",
            source="revision" if latest is not None else "default",
            max_age_seconds=resolver.ttl_seconds,
        ),
    )


@router.post("", response_model=EdgeLaneSettingsRevision)
async def create_settings_revision(
    request: Request,
    payload: AdminEdgeLaneSettingsRequest,
    _admin: AdminDep,
    session: SessionDep,
) -> EdgeLaneSettingsRevision:
    if payload.scope != GLOBAL_SCOPE:
        raise HTTPException(status_code=422, detail="scope must be '*'")
    if payload.confirmation != CONFIRMATION:
        raise HTTPException(
            status_code=409, detail=f"confirmation must be exactly {CONFIRMATION}"
        )
    latest = await latest_edge_lane_settings_revision(session)
    actual_revision = latest.revision if latest is not None else 0
    if payload.expected_revision != actual_revision:
        raise HTTPException(
            status_code=409,
            detail=(
                "edge lane settings changed; refresh before applying "
                f"(expected {payload.expected_revision}, current {actual_revision})"
            ),
        )
    settings = payload.settings
    try:
        row = await insert_edge_lane_settings_revision(
            session,
            parent_revision=actual_revision,
            scope=payload.scope,
            settings=settings.model_dump(mode="json"),
            checksum=_checksum(settings),
            reason=payload.reason.strip(),
            actor=payload.actor.strip(),
        )
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail="edge lane settings changed concurrently; refresh and retry",
        ) from error
    await session.refresh(row)
    _resolver(request).invalidate()
    logger.warning(
        "edge lane set to mode=%s by %s (revision %d): %s",
        settings.mode,
        row.actor,
        row.revision,
        row.reason,
    )
    return _revision(row)
