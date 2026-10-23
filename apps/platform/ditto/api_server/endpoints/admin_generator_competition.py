"""Audited operator control and inspection for the generator competition.

Settings are append-only, whole-object revisions guarded by
``expected_revision`` and an exact confirmation phrase naming the mode. Pack
detail exposes validation issues, the panel snapshot, per-case verdicts, and
the score summary; raw pack bytes are never served.
"""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.generator import (
    AdminGeneratorPackDetail,
    AdminGeneratorPackListResponse,
    AdminGeneratorPromotionCandidatesResponse,
    AdminGeneratorSettingsRequest,
    AdminGeneratorSettingsResponse,
    EffectiveGeneratorSettings,
    GeneratorCaseVerdictView,
    GeneratorPanelSlot,
    GeneratorPromotionCandidate,
    GeneratorSettingsRevision,
    ValidationReport,
    generator_settings_confirmation,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.api_server.endpoints.generator import pack_summary
from ditto.api_server.generator_competition import (
    DEFAULT_SETTINGS,
    settings_checksum,
    settings_from_row,
)
from ditto.db.models import GeneratorCompetitionSettingsRevision as RevisionRow
from ditto.db.queries.generator import (
    case_status_counts,
    get_pack,
    insert_generator_settings_revision,
    latest_generator_settings_revision,
    list_generator_settings_revisions,
    list_packs,
    list_promotion_candidates,
    list_verdicts,
    pack_counts_by_status,
)

router = APIRouter(prefix="/admin/generator-competition", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]
MIN_REASON_LENGTH = 8
MAX_ACTOR_LENGTH = 120


def _revision(row: RevisionRow) -> GeneratorSettingsRevision:
    settings, valid = settings_from_row(row)
    return GeneratorSettingsRevision(
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


@router.get("/settings", response_model=AdminGeneratorSettingsResponse)
async def get_settings(
    _admin: AdminDep, session: SessionDep
) -> AdminGeneratorSettingsResponse:
    """Current policy, append-only history, the default, and what is in force."""
    latest = await latest_generator_settings_revision(session)
    history = await list_generator_settings_revisions(session)
    current = _revision(latest) if latest is not None else None
    return AdminGeneratorSettingsResponse(
        current=[current] if current is not None else [],
        history=[_revision(row) for row in history],
        default=DEFAULT_SETTINGS,
        effective=EffectiveGeneratorSettings(
            revision=latest.revision if latest is not None else 0,
            settings=current.settings if current is not None else DEFAULT_SETTINGS,
            checksum=latest.checksum if latest is not None else "",
            source="revision"
            if current is not None and current.settings_valid
            else "default",
            settings_valid=current.settings_valid if current is not None else True,
        ),
    )


@router.post("/settings", response_model=GeneratorSettingsRevision)
async def create_settings_revision(
    payload: AdminGeneratorSettingsRequest, _admin: AdminDep, session: SessionDep
) -> GeneratorSettingsRevision:
    """Append one optimistic, confirmation-gated revision."""
    reason = payload.reason.strip()
    actor = payload.actor.strip()
    expected_confirmation = generator_settings_confirmation(payload.settings.mode)
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
    if payload.scope != "*":
        raise HTTPException(
            status_code=422,
            detail=(
                "generator competition settings are subnet-global; scope must be '*'"
            ),
        )
    if payload.confirmation != expected_confirmation:
        raise HTTPException(
            status_code=409,
            detail=f"confirmation must be exactly {expected_confirmation}",
        )
    latest = await latest_generator_settings_revision(session)
    actual = latest.revision if latest is not None else 0
    if payload.expected_revision != actual:
        raise HTTPException(
            status_code=409,
            detail=(
                "generator competition settings changed; refresh before applying "
                f"(expected {payload.expected_revision}, current {actual})"
            ),
        )
    try:
        row = await insert_generator_settings_revision(
            session,
            parent_revision=actual,
            settings=payload.settings.model_dump(mode="json"),
            checksum=settings_checksum(payload.settings),
            reason=reason,
            actor=actor,
        )
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail="generator competition settings changed concurrently; refresh",
        ) from exc
    await session.refresh(row)
    return _revision(row)


PackStatusFilter = Literal["queued", "leased", "scored", "invalid", "failed"]


@router.get("/packs", response_model=AdminGeneratorPackListResponse)
async def admin_list_packs(
    _admin: AdminDep,
    session: SessionDep,
    hotkey: Annotated[str | None, Query(max_length=64)] = None,
    status: PackStatusFilter | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> AdminGeneratorPackListResponse:
    rows = await list_packs(session, miner_hotkey=hotkey, status=status, limit=limit)
    counts = await case_status_counts(session, [row.pack_id for row in rows])
    return AdminGeneratorPackListResponse(
        packs=[pack_summary(row, counts.get(row.pack_id)) for row in rows],
        counts_by_status=await pack_counts_by_status(session),
    )


@router.get("/packs/{pack_id}", response_model=AdminGeneratorPackDetail)
async def admin_pack_detail(
    pack_id: UUID, _admin: AdminDep, session: SessionDep
) -> AdminGeneratorPackDetail:
    row = await get_pack(session, pack_id)
    if row is None:
        raise HTTPException(status_code=404, detail="unknown pack")
    counts = await case_status_counts(session, [row.pack_id])
    verdicts = await list_verdicts(session, row.pack_id)
    validation = None
    if row.validation is not None:
        try:
            validation = ValidationReport.model_validate(row.validation)
        except ValidationError:
            validation = None
    return AdminGeneratorPackDetail(
        summary=pack_summary(row, counts.get(row.pack_id)),
        owner_key=row.owner_key,
        attempts=row.attempts,
        not_before=row.not_before,
        lease_expires_at=row.lease_expires_at,
        settings_revision=row.settings_revision,
        scorer_version=row.scorer_version,
        panel=[GeneratorPanelSlot(**slot) for slot in row.panel or ()] or None,
        validation=validation,
        score_summary=row.score_summary,
        verdicts=[
            GeneratorCaseVerdictView(
                case_id=v.case_id,
                position=v.position,
                fingerprint=v.fingerprint,
                status=v.status,  # type: ignore[arg-type]
                panel_size=v.panel_size,
                solvers=v.solvers,
                solver_owners=v.solver_owners,
                mean_score=v.mean_score,
                champion_failed=v.champion_failed,
                credit=v.credit,
            )
            for v in verdicts
        ],
    )


@router.get(
    "/promotion-candidates", response_model=AdminGeneratorPromotionCandidatesResponse
)
async def promotion_candidates(
    _admin: AdminDep,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> AdminGeneratorPromotionCandidatesResponse:
    """Discriminating cases from scored packs, highest credit first."""
    rows = await list_promotion_candidates(session, limit=limit)
    return AdminGeneratorPromotionCandidatesResponse(
        candidates=[
            GeneratorPromotionCandidate(
                pack_id=pack.pack_id,
                miner_hotkey=pack.miner_hotkey,
                case_id=verdict.case_id,
                fingerprint=verdict.fingerprint,
                mean_score=verdict.mean_score,
                solver_owners=verdict.solver_owners,
                champion_failed=verdict.champion_failed,
                credit=verdict.credit,
                scored_at=pack.scored_at,  # type: ignore[arg-type]
            )
            for verdict, pack in rows
        ]
    )
