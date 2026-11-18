"""Audited operator control for the model_context (CPM) competition.

Settings are append-only, whole-object revisions guarded by
``expected_revision`` and an exact confirmation phrase naming the mode (the
generator competition's pattern). A forced season rotation reads the
finalized head like the scheduled one and needs its own confirmation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.model_context import (
    ROTATE_SEASON_CONFIRMATION,
    AdminModelContextScorerStatus,
    AdminModelContextSettingsRequest,
    AdminModelContextSettingsResponse,
    AdminRotateSeasonRequest,
    EffectiveModelContextSettings,
    ModelContextScorerHeartbeatView,
    ModelContextSeasonView,
    ModelContextSettingsRevision,
    model_context_settings_confirmation,
)
from ditto.api_server.dependencies import get_chain_client, get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.api_server.model_context_competition import (
    DEFAULT_SETTINGS,
    current_season,
    latest_settings_revision,
    list_settings_revisions,
    load_settings,
    rotate_in_session,
    season_view,
    settings_checksum,
    settings_from_row,
)
from ditto.chain.errors import ChainError
from ditto.db.models import (
    ModelContextRun,
    ModelContextScorerHeartbeat,
    ModelContextSeason,
    ModelContextSubmission,
)
from ditto.db.models import ModelContextSettingsRevision as RevisionRow

if TYPE_CHECKING:
    from ditto.chain.client import ChainClient

router = APIRouter(prefix="/admin/model-context", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]
ChainDep = Annotated["ChainClient", Depends(get_chain_client)]
MIN_REASON_LENGTH = 8
MAX_ACTOR_LENGTH = 120


def _revision(row: RevisionRow) -> ModelContextSettingsRevision:
    settings, valid = settings_from_row(row)
    return ModelContextSettingsRevision(
        revision=row.revision,
        parent_revision=row.parent_revision,
        scope=row.scope,
        settings=settings,
        settings_valid=valid,
        reason=row.reason,
        actor=row.actor,
        created_at=row.created_at,
        checksum=row.checksum,
    )


def _reason_and_actor(reason: str, actor: str) -> tuple[str, str]:
    reason, actor = reason.strip(), actor.strip()
    if len(reason) < MIN_REASON_LENGTH:
        raise HTTPException(
            status_code=422,
            detail=(
                f"reason must be at least {MIN_REASON_LENGTH} characters after trimming"
            ),
        )
    if not 1 <= len(actor) <= MAX_ACTOR_LENGTH:
        raise HTTPException(
            status_code=422,
            detail=f"actor must be 1-{MAX_ACTOR_LENGTH} characters after trimming",
        )
    return reason, actor


@router.get("/settings", response_model=AdminModelContextSettingsResponse)
async def get_settings(
    _admin: AdminDep, session: SessionDep
) -> AdminModelContextSettingsResponse:
    """Current policy, append-only history, the default, and what is in force."""
    latest = await latest_settings_revision(session)
    current = _revision(latest) if latest is not None else None
    return AdminModelContextSettingsResponse(
        current=[current] if current is not None else [],
        history=[_revision(row) for row in await list_settings_revisions(session)],
        default=DEFAULT_SETTINGS,
        effective=EffectiveModelContextSettings(
            revision=latest.revision if latest is not None else 0,
            settings=current.settings if current is not None else DEFAULT_SETTINGS,
            checksum=latest.checksum if latest is not None else "",
            source=(
                "revision"
                if current is not None and current.settings_valid
                else "default"
            ),
            settings_valid=current.settings_valid if current is not None else True,
        ),
    )


@router.post("/settings", response_model=ModelContextSettingsRevision)
async def create_settings_revision(
    payload: AdminModelContextSettingsRequest, _admin: AdminDep, session: SessionDep
) -> ModelContextSettingsRevision:
    """Append one optimistic, confirmation-gated revision."""
    reason, actor = _reason_and_actor(payload.reason, payload.actor)
    if payload.scope != "*":
        raise HTTPException(
            status_code=422,
            detail="model_context settings are subnet-global; scope must be '*'",
        )
    expected_confirmation = model_context_settings_confirmation(payload.settings.mode)
    if payload.confirmation != expected_confirmation:
        raise HTTPException(
            status_code=409,
            detail=f"confirmation must be exactly {expected_confirmation}",
        )
    if payload.settings.upload_fee_required:
        raise HTTPException(
            status_code=422,
            detail=(
                "upload_fee_required is not available: the model_context fee "
                "path is not built in v1"
            ),
        )
    latest = await latest_settings_revision(session)
    actual = latest.revision if latest is not None else 0
    if payload.expected_revision != actual:
        raise HTTPException(
            status_code=409,
            detail=(
                "model_context settings changed; refresh before applying "
                f"(expected {payload.expected_revision}, current {actual})"
            ),
        )
    row = RevisionRow(
        parent_revision=actual,
        scope="*",
        settings=payload.settings.model_dump(mode="json"),
        checksum=settings_checksum(payload.settings),
        reason=reason,
        actor=actor,
    )
    session.add(row)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail="model_context settings changed concurrently; refresh",
        ) from exc
    await session.refresh(row)
    return _revision(row)


@router.post("/seasons/rotate", response_model=ModelContextSeasonView)
async def rotate(
    payload: AdminRotateSeasonRequest,
    _admin: AdminDep,
    session: SessionDep,
    chain: ChainDep,
) -> ModelContextSeasonView:
    """Close the open season now and open the next on the finalized head."""
    _reason_and_actor(payload.reason, payload.actor)
    if payload.confirmation != ROTATE_SEASON_CONFIRMATION:
        raise HTTPException(
            status_code=409,
            detail=f"confirmation must be exactly {ROTATE_SEASON_CONFIRMATION}",
        )
    if (await load_settings(session)).settings.mode == "off":
        raise HTTPException(
            status_code=409, detail="model_context mode is off; no season opens"
        )
    try:
        block = await chain.get_finalized_block()
    except ChainError as exc:
        raise HTTPException(
            status_code=503, detail="chain unavailable; retry shortly"
        ) from exc
    if session.in_transaction():
        await session.rollback()
    async with session.begin():
        number = await rotate_in_session(session, block=block, force=True)
    if number is None:
        raise HTTPException(
            status_code=409, detail="model_context mode is off; no season opens"
        )
    row = await session.get(ModelContextSeason, number)
    assert row is not None
    return season_view(row)


@router.get("/scorer", response_model=AdminModelContextScorerStatus)
async def scorer_status(
    _admin: AdminDep, session: SessionDep
) -> AdminModelContextScorerStatus:
    """Scorer heartbeats and the run and screening backlog."""
    heartbeats = list(
        await session.scalars(
            select(ModelContextScorerHeartbeat).order_by(
                ModelContextScorerHeartbeat.last_seen_at.desc()
            )
        )
    )
    counts: dict[str, int] = {
        str(status): int(count)
        for status, count in (
            await session.execute(
                select(ModelContextRun.status, func.count())
                .where(ModelContextRun.status.in_(("queued", "leased")))
                .group_by(ModelContextRun.status)
            )
        ).all()
    }
    screening = await session.scalar(
        select(func.count()).where(ModelContextSubmission.status == "screening")
    )
    season = await current_season(session)
    return AdminModelContextScorerStatus(
        scorers=[
            ModelContextScorerHeartbeatView(
                scorer_id=row.scorer_id,
                scorer_version=row.scorer_version,
                gpu_sku=row.gpu_sku,
                last_poll=row.last_poll,
                first_seen_at=row.first_seen_at,
                last_seen_at=row.last_seen_at,
            )
            for row in heartbeats
        ],
        queued_runs=int(counts.get("queued", 0)),
        leased_runs=int(counts.get("leased", 0)),
        screening_submissions=int(screening or 0),
        current_season=season.season if season is not None else None,
    )
