"""Audited operator control and inspection for the model_context (CPM) competition.

Settings are append-only, whole-object revisions guarded by
``expected_revision`` and an exact confirmation phrase naming the mode (the
generator competition's pattern). A forced season rotation reads the
finalized head like the scheduled one and needs its own confirmation.
Submission and run reads serve everything Platform stores except a run's
lease id (a scorer credential); weights and season seeds are public by
contract. Rescore and reject are guarded by the status the operator read and
an exact confirmation, and every rotation, rescore and rejection is appended
to ``model_context_operator_actions`` with its actor and reason.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.model_context import (
    REJECT_SUBMISSION_CONFIRMATION,
    RESCORE_SUBMISSION_CONFIRMATION,
    ROTATE_SEASON_CONFIRMATION,
    AdminModelContextRunDetail,
    AdminModelContextRunView,
    AdminModelContextScorerStatus,
    AdminModelContextSettingsRequest,
    AdminModelContextSettingsResponse,
    AdminModelContextSubmissionActionRequest,
    AdminModelContextSubmissionDetail,
    AdminModelContextSubmissionListResponse,
    AdminRotateSeasonRequest,
    EffectiveModelContextSettings,
    ModelContextOperatorActionView,
    ModelContextScorerHeartbeatView,
    ModelContextSeasonView,
    ModelContextSettingsRevision,
    SubmissionStatus,
    model_context_settings_confirmation,
)
from ditto.api_server.dependencies import get_chain_client, get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.api_server.model_context_competition import (
    ACTIVE_STATUSES,
    DEFAULT_SETTINGS,
    current_season,
    enqueue_submission_run,
    latest_runs,
    latest_settings_revision,
    list_settings_revisions,
    load_settings,
    lock_hotkey,
    reject_submission,
    rotate_in_session,
    season_view,
    settings_checksum,
    settings_from_row,
    submission_summary,
)
from ditto.chain.errors import ChainError
from ditto.db.models import (
    ModelContextOperatorAction,
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


def _record_action(
    session: AsyncSession,
    *,
    action: str,
    subject: str,
    actor: str,
    reason: str,
    detail: dict | None = None,
) -> None:
    """Append one operator action; it commits with the action itself."""
    session.add(
        ModelContextOperatorAction(
            action=action, subject=subject, actor=actor, reason=reason, detail=detail
        )
    )


@router.post("/seasons/rotate", response_model=ModelContextSeasonView)
async def rotate(
    payload: AdminRotateSeasonRequest,
    _admin: AdminDep,
    session: SessionDep,
    chain: ChainDep,
) -> ModelContextSeasonView:
    """Close the open season now and open the next on the finalized head."""
    reason, actor = _reason_and_actor(payload.reason, payload.actor)
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
        previous = await current_season(session)
        closed = previous.season if previous is not None else None
        number = await rotate_in_session(session, block=block, force=True)
        if number is not None:
            _record_action(
                session,
                action="rotate_season",
                subject=f"season:{number}",
                actor=actor,
                reason=reason,
                detail={"closed_season": closed, "open_block": block.number},
            )
    if number is None:
        raise HTTPException(
            status_code=409, detail="model_context mode is off; no season opens"
        )
    row = await session.get(ModelContextSeason, number)
    assert row is not None
    return season_view(row)


@router.get("/scorer", response_model=AdminModelContextScorerStatus)
async def scorer_status(
    request: Request, _admin: AdminDep, session: SessionDep
) -> AdminModelContextScorerStatus:
    """Scorer heartbeats, the run and screening backlog, and this process's
    season and screening loop."""
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
    loop = getattr(request.app.state, "model_context_loop", None)
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
        loop_running=loop is not None,
        loop_last_error=getattr(loop, "last_error", None),
    )


# --- submissions and runs ----------------------------------------------------


def _run_view(run: ModelContextRun) -> AdminModelContextRunView:
    return AdminModelContextRunView(
        run_id=run.run_id,
        kind=run.kind,  # type: ignore[arg-type]
        submission_id=run.submission_id,
        base_id=run.base_id,
        season=run.season,
        cpm_score_version=run.cpm_score_version,
        status=run.status,  # type: ignore[arg-type]
        score=run.score,
        components=run.components,
        per_seed=run.per_seed,
        scorer_id=run.scorer_id,
        scorer_version=run.scorer_version,
        gpu_sku=run.gpu_sku,
        attempts=run.attempts,
        lease_expires_at=run.lease_expires_at,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
        fault=run.fault,
    )


def _action_view(row: ModelContextOperatorAction) -> ModelContextOperatorActionView:
    return ModelContextOperatorActionView(
        id=row.id,
        action=row.action,  # type: ignore[arg-type]
        subject=row.subject,
        actor=row.actor,
        reason=row.reason,
        detail=row.detail,
        created_at=row.created_at,
    )


@router.get("/submissions", response_model=AdminModelContextSubmissionListResponse)
async def admin_list_submissions(
    _admin: AdminDep,
    session: SessionDep,
    hotkey: Annotated[str | None, Query(max_length=64)] = None,
    status: SubmissionStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AdminModelContextSubmissionListResponse:
    """Every submission newest first, any hotkey, with its latest run."""
    query = select(ModelContextSubmission)
    if hotkey is not None:
        query = query.where(ModelContextSubmission.hotkey == hotkey)
    if status is not None:
        query = query.where(ModelContextSubmission.status == status)
    rows = list(
        await session.scalars(
            query.order_by(
                ModelContextSubmission.created_at.desc(),
                ModelContextSubmission.submission_id,
            )
            .limit(limit)
            .offset(offset)
        )
    )
    latest = await latest_runs(session, [row.submission_id for row in rows])
    counts = {
        str(value): int(count)
        for value, count in (
            await session.execute(
                select(ModelContextSubmission.status, func.count()).group_by(
                    ModelContextSubmission.status
                )
            )
        ).all()
    }
    return AdminModelContextSubmissionListResponse(
        submissions=[
            submission_summary(row, latest.get(row.submission_id)) for row in rows
        ],
        counts_by_status=counts,
    )


@router.get(
    "/submissions/{submission_id}", response_model=AdminModelContextSubmissionDetail
)
async def admin_submission_detail(
    submission_id: UUID, _admin: AdminDep, session: SessionDep
) -> AdminModelContextSubmissionDetail:
    """One submission: manifest, screening, every run and the operator audit."""
    row = await session.get(ModelContextSubmission, submission_id)
    if row is None:
        raise HTTPException(status_code=404, detail="unknown model_context submission")
    runs = list(
        await session.scalars(
            select(ModelContextRun)
            .where(ModelContextRun.submission_id == submission_id)
            .order_by(ModelContextRun.created_at.desc(), ModelContextRun.run_id)
        )
    )
    actions = list(
        await session.scalars(
            select(ModelContextOperatorAction)
            .where(ModelContextOperatorAction.subject == str(submission_id))
            .order_by(
                ModelContextOperatorAction.created_at.desc(),
                ModelContextOperatorAction.id.desc(),
            )
        )
    )
    return AdminModelContextSubmissionDetail(
        summary=submission_summary(row, runs[0] if runs else None),
        manifest=dict(row.manifest),
        total_bytes=row.total_bytes,
        screening_detail=row.screening_detail,
        screening_attempts=row.screening_attempts,
        screening_lease_until=row.screening_lease_until,
        updated_at=row.updated_at,
        runs=[_run_view(run) for run in runs],
        actions=[_action_view(action) for action in actions],
    )


@router.get("/runs/{run_id}", response_model=AdminModelContextRunDetail)
async def admin_run_detail(
    run_id: UUID, _admin: AdminDep, session: SessionDep
) -> AdminModelContextRunDetail:
    """One run (submission or reference), its season and its base floor."""
    run = await session.get(ModelContextRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="unknown model_context run")
    submission = (
        await session.get(ModelContextSubmission, run.submission_id)
        if run.submission_id is not None
        else None
    )
    season = await session.get(ModelContextSeason, run.season)
    floors = dict(season.base_floors or {}) if season is not None else {}
    floor = floors.get(run.base_id)
    return AdminModelContextRunDetail(
        run=_run_view(run),
        submission=(
            submission_summary(submission, None) if submission is not None else None
        ),
        season=season_view(season) if season is not None else None,
        floor=float(floor) if floor is not None else None,
    )


async def _locked_submission(
    session: AsyncSession,
    submission_id: UUID,
    payload: AdminModelContextSubmissionActionRequest,
    *,
    confirmation: str,
) -> tuple[ModelContextSubmission, str, str]:
    """Check the confirmation, then lock the hotkey and the row and check the
    status the operator read (the caller commits or rolls back)."""
    reason, actor = _reason_and_actor(payload.reason, payload.actor)
    if payload.confirmation != confirmation:
        raise HTTPException(
            status_code=409, detail=f"confirmation must be exactly {confirmation}"
        )
    hotkey = await session.scalar(
        select(ModelContextSubmission.hotkey).where(
            ModelContextSubmission.submission_id == submission_id
        )
    )
    if hotkey is None:
        raise HTTPException(status_code=404, detail="unknown model_context submission")
    # Screening takes this lock before superseding, so a ruling never races
    # a newer submission of the hotkey becoming active.
    await lock_hotkey(session, hotkey)
    row = await session.get(ModelContextSubmission, submission_id, with_for_update=True)
    assert row is not None
    if row.status != payload.expected_status:
        status = row.status
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail=(
                f"submission is {status}, not {payload.expected_status}; "
                "re-read it and retry"
            ),
        )
    return row, reason, actor


@router.post(
    "/submissions/{submission_id}/rescore",
    response_model=AdminModelContextSubmissionDetail,
)
async def rescore_submission(
    submission_id: UUID,
    payload: AdminModelContextSubmissionActionRequest,
    _admin: AdminDep,
    session: SessionDep,
) -> AdminModelContextSubmissionDetail:
    """Queue one more run of an active submission in the open season.

    For a run that failed for a reason that was not the model's (a
    ``nondeterministic`` verdict traced to the host, an abandoned run). The
    submission keeps its status, and a score it already has this season
    stays on the board until the new run lands; the latest scored run of the
    season then stands. Refused while the lane is off, no season is open, or
    a run of it is already queued or leased.
    """
    if session.in_transaction():
        await session.rollback()
    row, reason, actor = await _locked_submission(
        session, submission_id, payload, confirmation=RESCORE_SUBMISSION_CONFIRMATION
    )
    refusal: str | None = None
    season = None
    if row.status not in ACTIVE_STATUSES:
        refusal = (
            f"submission is {row.status}; only an active (queued or scored) "
            "submission is rescored"
        )
    elif (await load_settings(session)).settings.mode == "off":
        refusal = "model_context mode is off; nothing is scored"
    else:
        season = await current_season(session)
        if season is None:
            refusal = "no season is open; rotate_model_context_season opens one"
    if refusal is not None or season is None:
        await session.rollback()
        raise HTTPException(status_code=409, detail=refusal)
    number = season.season
    now = datetime.now(UTC)
    if not await enqueue_submission_run(session, row, number, now=now):
        # A rollback expires the loaded rows; the message uses the number.
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail=(
                f"a run of this submission is already queued or leased in "
                f"season {number}"
            ),
        )
    row.updated_at = now
    _record_action(
        session,
        action="rescore",
        subject=str(row.submission_id),
        actor=actor,
        reason=reason,
        detail={"status": row.status, "season": number},
    )
    await session.commit()
    return await admin_submission_detail(submission_id, _admin, session)


@router.post(
    "/submissions/{submission_id}/reject",
    response_model=AdminModelContextSubmissionDetail,
)
async def reject_submission_by_operator(
    submission_id: UUID,
    payload: AdminModelContextSubmissionActionRequest,
    _admin: AdminDep,
    session: SessionDep,
) -> AdminModelContextSubmissionDetail:
    """Reject a submission by operator ruling (abuse, a copy screening missed).

    It leaves the board and ledger at once and its queued or leased runs are
    abandoned (a scorer's late result is refused). A ``screening`` submission
    can be rejected too: the screen in flight finds it no longer screening and
    changes nothing. The hotkey's earlier submission is not restored; the
    miner can submit again after the cooldown.
    """
    if session.in_transaction():
        await session.rollback()
    row, reason, actor = await _locked_submission(
        session, submission_id, payload, confirmation=REJECT_SUBMISSION_CONFIRMATION
    )
    if row.status == "rejected":
        await session.rollback()
        raise HTTPException(status_code=409, detail="submission is already rejected")
    previous = row.status
    await reject_submission(
        session, row, f"operator ({actor}): {reason}", now=datetime.now(UTC)
    )
    _record_action(
        session,
        action="reject",
        subject=str(row.submission_id),
        actor=actor,
        reason=reason,
        detail={"previous_status": previous},
    )
    await session.commit()
    return await admin_submission_detail(submission_id, _admin, session)
