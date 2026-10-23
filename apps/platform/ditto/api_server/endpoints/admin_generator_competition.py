"""Audited operator control and inspection for the generator competition.

Settings are append-only, whole-object revisions guarded by
``expected_revision`` and an exact confirmation phrase naming the mode.
Submission and run detail expose the evaluation (build, per-seed validation,
determinism, probe), the panel snapshot, per-case verdicts, and the score
summary. Generator source is never served, a run's seeds are revealed only
after it finished, and probe seeds are never served at all.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.generator import (
    PROBE_ROTATION_CONFIRMATION,
    REJECT_SUBMISSION_CONFIRMATION,
    AdminGeneratorProbeResponse,
    AdminGeneratorProbeRotateRequest,
    AdminGeneratorPromotionCandidatesResponse,
    AdminGeneratorRunDetail,
    AdminGeneratorSettingsRequest,
    AdminGeneratorSettingsResponse,
    AdminGeneratorSubmissionActionRequest,
    AdminGeneratorSubmissionDetail,
    AdminGeneratorSubmissionListResponse,
    EffectiveGeneratorSettings,
    GeneratorCaseVerdictView,
    GeneratorPanelSlot,
    GeneratorProbeEpochView,
    GeneratorPromotionCandidate,
    GeneratorSettingsRevision,
    generator_settings_confirmation,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.api_server.generator_competition import (
    DEFAULT_SETTINGS,
    run_summary,
    settings_checksum,
    settings_from_row,
    submission_summary,
)
from ditto.db.models import GeneratorCompetitionSettingsRevision as RevisionRow
from ditto.db.models import GeneratorProbeEpoch
from ditto.db.queries.generator import (
    abandon_run,
    case_status_counts,
    clear_lease,
    get_run,
    get_submission,
    insert_generator_settings_revision,
    latest_generator_settings_revision,
    latest_runs,
    list_generator_settings_revisions,
    list_probe_epochs,
    list_promotion_candidates,
    list_runs,
    list_submissions,
    list_verdicts,
    rotate_probe_epoch,
    submission_counts_by_status,
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


SubmissionStatusFilter = Literal["active", "superseded", "rejected"]


@router.get("/submissions", response_model=AdminGeneratorSubmissionListResponse)
async def admin_list_submissions(
    _admin: AdminDep,
    session: SessionDep,
    hotkey: Annotated[str | None, Query(max_length=64)] = None,
    status: SubmissionStatusFilter | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AdminGeneratorSubmissionListResponse:
    rows = await list_submissions(
        session, miner_hotkey=hotkey, status=status, limit=limit, offset=offset
    )
    latest = await latest_runs(session, [row.submission_id for row in rows])
    counts = await case_status_counts(session, [run.run_id for run in latest.values()])
    return AdminGeneratorSubmissionListResponse(
        submissions=[
            submission_summary(
                row,
                latest.get(row.submission_id),
                counts.get(latest[row.submission_id].run_id)
                if row.submission_id in latest
                else None,
            )
            for row in rows
        ],
        counts_by_status=await submission_counts_by_status(session),
    )


@router.get(
    "/submissions/{submission_id}", response_model=AdminGeneratorSubmissionDetail
)
async def admin_submission_detail(
    submission_id: UUID, _admin: AdminDep, session: SessionDep
) -> AdminGeneratorSubmissionDetail:
    row = await get_submission(session, submission_id)
    if row is None:
        raise HTTPException(status_code=404, detail="unknown submission")
    runs = await list_runs(session, submission_id)
    counts = await case_status_counts(session, [run.run_id for run in runs])
    latest = next((run for run in runs if run.status != "abandoned"), None)
    return AdminGeneratorSubmissionDetail(
        summary=submission_summary(
            row, latest, counts.get(latest.run_id) if latest else None
        ),
        owner_key=row.owner_key,
        attempts=row.attempts,
        lease_run_id=row.lease_run_id,
        lease_expires_at=row.lease_expires_at,
        runs=[
            summary
            for run in runs
            if (summary := run_summary(run, counts.get(run.run_id))) is not None
        ],
    )


async def _locked_submission(
    session: AsyncSession,
    submission_id: UUID,
    payload: AdminGeneratorSubmissionActionRequest,
):
    reason = payload.reason.strip()
    actor = payload.actor.strip()
    if len(reason) < MIN_REASON_LENGTH or not 1 <= len(actor) <= MAX_ACTOR_LENGTH:
        raise HTTPException(status_code=422, detail="reason and actor are required")
    row = await get_submission(session, submission_id, for_update=True)
    if row is None:
        raise HTTPException(status_code=404, detail="unknown submission")
    if row.status != payload.expected_status:
        raise HTTPException(
            status_code=409,
            detail=(
                f"submission is {row.status}, not {payload.expected_status}; "
                "re-read it and retry"
            ),
        )
    return row, reason, actor


@router.post(
    "/submissions/{submission_id}/rescore",
    response_model=AdminGeneratorSubmissionDetail,
)
async def rescore_submission(
    submission_id: UUID,
    payload: AdminGeneratorSubmissionActionRequest,
    _admin: AdminDep,
    session: SessionDep,
) -> AdminGeneratorSubmissionDetail:
    """Make an active generator due now, so its next run starts on fresh seeds
    against the current panel. An in-flight run is left to finish."""
    row, _reason, _actor = await _locked_submission(session, submission_id, payload)
    if row.status != "active":
        raise HTTPException(status_code=409, detail="only an active generator runs")
    now = datetime.now(UTC)
    row.next_run_at = now
    row.attempts = 0
    row.updated_at = now
    await session.commit()
    return await admin_submission_detail(submission_id, _admin, session)


@router.post(
    "/submissions/{submission_id}/reject",
    response_model=AdminGeneratorSubmissionDetail,
)
async def reject_submission(
    submission_id: UUID,
    payload: AdminGeneratorSubmissionActionRequest,
    _admin: AdminDep,
    session: SessionDep,
) -> AdminGeneratorSubmissionDetail:
    """Reject a generator by operator ruling (abuse, a copy the probe missed).

    Its runs stop counting on the ledger at once, an in-flight run is abandoned,
    and it is never leased again. The miner can still upload a new generator.
    """
    if payload.confirmation != REJECT_SUBMISSION_CONFIRMATION:
        raise HTTPException(
            status_code=409,
            detail=f"confirmation must be exactly {REJECT_SUBMISSION_CONFIRMATION}",
        )
    row, reason, actor = await _locked_submission(session, submission_id, payload)
    if row.status == "rejected":
        raise HTTPException(status_code=409, detail="submission is already rejected")
    now = datetime.now(UTC)
    if row.lease_run_id is not None:
        await abandon_run(session, row.lease_run_id, now=now)
        clear_lease(row, now=now)
    row.status = "rejected"
    row.status_reason = f"operator ({actor}): {reason}"[:1000]
    row.updated_at = now
    await session.commit()
    return await admin_submission_detail(submission_id, _admin, session)


@router.get("/runs/{run_id}", response_model=AdminGeneratorRunDetail)
async def admin_run_detail(
    run_id: UUID, _admin: AdminDep, session: SessionDep
) -> AdminGeneratorRunDetail:
    run = await get_run(session, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="unknown run")
    row = await get_submission(session, run.submission_id)
    assert row is not None
    counts = await case_status_counts(session, [run.run_id])
    verdicts = await list_verdicts(session, run.run_id)
    summary = run_summary(run, counts.get(run.run_id))
    assert summary is not None
    return AdminGeneratorRunDetail(
        summary=summary,
        submission_id=row.submission_id,
        miner_hotkey=row.miner_hotkey,
        # A leased run's seeds are live secrets; finished runs reveal them so an
        # operator can reproduce the exact packs.
        seeds=list(run.seeds) if run.status != "leased" else None,
        probe_epoch=run.probe_epoch,
        cases_per_seed=run.cases_per_seed,
        settings_revision=run.settings_revision,
        scorer_version=run.scorer_version,
        panel=[GeneratorPanelSlot(**slot) for slot in run.panel or ()],
        evaluation=run.evaluation,
        score_summary=run.score_summary,
        verdicts=[
            GeneratorCaseVerdictView(
                seed_index=v.seed_index,
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


def _epoch_view(epoch: GeneratorProbeEpoch, probed: int) -> GeneratorProbeEpochView:
    return GeneratorProbeEpochView(
        epoch=epoch.epoch,
        reason=epoch.reason,
        actor=epoch.actor,
        created_at=epoch.created_at,
        retired_at=epoch.retired_at,
        probed_submissions=probed,
    )


@router.get("/probe", response_model=AdminGeneratorProbeResponse)
async def get_probe(
    _admin: AdminDep, session: SessionDep
) -> AdminGeneratorProbeResponse:
    """Probe epochs and how many submissions each has compared (never the seed)."""
    epochs = await list_probe_epochs(session)
    current = next((_epoch_view(e, n) for e, n in epochs if e.retired_at is None), None)
    return AdminGeneratorProbeResponse(
        current=current, history=[_epoch_view(e, n) for e, n in epochs]
    )


@router.post("/probe/rotate", response_model=AdminGeneratorProbeResponse)
async def rotate_probe(
    payload: AdminGeneratorProbeRotateRequest, _admin: AdminDep, session: SessionDep
) -> AdminGeneratorProbeResponse:
    """Retire the probe seed (for example after a suspected leak).

    Copy detection only compares submissions probed in the same epoch, so every
    active submission is re-probed on its next run.
    """
    reason = payload.reason.strip()
    actor = payload.actor.strip()
    if len(reason) < MIN_REASON_LENGTH or not 1 <= len(actor) <= MAX_ACTOR_LENGTH:
        raise HTTPException(status_code=422, detail="reason and actor are required")
    if payload.confirmation != PROBE_ROTATION_CONFIRMATION:
        raise HTTPException(
            status_code=409,
            detail=f"confirmation must be exactly {PROBE_ROTATION_CONFIRMATION}",
        )
    try:
        await rotate_probe_epoch(
            session, reason=reason, actor=actor, now=datetime.now(UTC)
        )
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409, detail="probe epoch rotated concurrently; refresh"
        ) from exc
    return await get_probe(_admin, session)


@router.get(
    "/promotion-candidates", response_model=AdminGeneratorPromotionCandidatesResponse
)
async def promotion_candidates(
    _admin: AdminDep,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AdminGeneratorPromotionCandidatesResponse:
    """Discriminating cases from scored runs, highest credit first."""
    rows = await list_promotion_candidates(session, limit=limit, offset=offset)
    return AdminGeneratorPromotionCandidatesResponse(
        candidates=[
            GeneratorPromotionCandidate(
                run_id=run.run_id,
                submission_id=sub.submission_id,
                miner_hotkey=sub.miner_hotkey,
                seed_index=verdict.seed_index,
                case_id=verdict.case_id,
                fingerprint=verdict.fingerprint,
                mean_score=verdict.mean_score,
                solver_owners=verdict.solver_owners,
                champion_failed=verdict.champion_failed,
                credit=verdict.credit,
                scored_at=run.finished_at,  # type: ignore[arg-type]
            )
            for verdict, run, sub in rows
        ]
    )
