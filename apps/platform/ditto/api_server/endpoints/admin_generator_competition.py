"""Audited operator control and inspection for the generator competition.

Settings are append-only, whole-object revisions guarded by
``expected_revision`` and an exact confirmation phrase naming the mode.
Submission and qualification-run detail expose the evaluation (build,
per-seed validation, determinism, probe) and its outcome. Generator source is
never served, a run's seeds are revealed only after it finished, and probe
seeds are never served at all. Round control lives in
``admin_generator_rounds``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.generator import (
    PROBE_ROTATION_CONFIRMATION,
    REINSTATE_SUBMISSION_CONFIRMATION,
    REJECT_SUBMISSION_CONFIRMATION,
    AdminGeneratorProbeResponse,
    AdminGeneratorProbeRotateRequest,
    AdminGeneratorPromotionCandidatesResponse,
    AdminGeneratorReinstateRequest,
    AdminGeneratorRunDetail,
    AdminGeneratorScorerStatus,
    AdminGeneratorSettingsRequest,
    AdminGeneratorSettingsResponse,
    AdminGeneratorSubmissionActionRequest,
    AdminGeneratorSubmissionDetail,
    AdminGeneratorSubmissionListResponse,
    EffectiveGeneratorSettings,
    GeneratorCaseVerdictView,
    GeneratorProbeEpochView,
    GeneratorPromotionCandidate,
    GeneratorScorerHeartbeatView,
    GeneratorSettingsRevision,
    generator_settings_confirmation,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.api_server.generator_competition import (
    DEFAULT_SETTINGS,
    load_settings,
    run_summary,
    settings_checksum,
    settings_from_row,
    submission_summary,
)
from ditto.api_server.generator_round_reservation import (
    invalidate_round_reservation,
)
from ditto.api_server.generator_rounds import active_round_contract, void_round
from ditto.api_server.generator_scorer_capability import (
    SCORER_CAPABILITY_TTL,
    grading_versions,
    is_live,
    list_heartbeats,
    live_scorer,
    refusal,
)
from ditto.db.models import GeneratorCompetitionSettingsRevision as RevisionRow
from ditto.db.models import (
    GeneratorOperatorAction,
    GeneratorProbeEpoch,
    GeneratorRound,
    GeneratorRoundCaseVerdict,
    GeneratorSubmission,
)
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
    list_runs,
    list_submissions,
    list_verdicts,
    lock_hotkey_uploads,
    record_operator_action,
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


async def _retire_rounds_for_interval_change(
    session: AsyncSession, *, old: int, new: int, actor: str, reason: str
) -> None:
    """A round's index is its first epoch divided by the interval, so a new
    interval would remap rounds already in flight. A round that has opened
    (seeded, generating or running) blocks the change; a committed round that
    never opened holds no seed and is voided in this same transaction."""
    open_rows = list(
        await session.scalars(
            select(GeneratorRound)
            .where(GeneratorRound.status.in_(("committed", "generating", "running")))
            .with_for_update()
        )
    )
    started = [r.round_index for r in open_rows if r.status != "committed"]
    if started:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail=(
                f"epochs_per_round cannot change while round(s) {started} are "
                "open; let them finish or void them, then retry"
            ),
        )
    now = datetime.now(UTC)
    for row in open_rows:
        void_round(row, f"round interval changed from {old} to {new}", now=now)
        record_operator_action(
            session,
            action="void",
            subject=f"round:{row.round_index}",
            actor=actor,
            reason=f"epochs_per_round {old} -> {new}: {reason}",
        )


@router.post("/settings", response_model=GeneratorSettingsRevision)
async def create_settings_revision(
    request: Request,
    payload: AdminGeneratorSettingsRequest,
    _admin: AdminDep,
    session: SessionDep,
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
    if payload.settings.mode != "off" and not payload.override_scorer_capability:
        refused = refusal(
            await live_scorer(session, now=datetime.now(UTC)),
            payload.settings.cases_per_seed,
        )
        if refused is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{refused}: redeploy the scorer first, or pass "
                    "override_scorer_capability to apply it anyway (its rounds "
                    "void and its qualifications wait until a capable scorer polls)"
                ),
            )
    current = (await load_settings(session)).settings
    if payload.settings.epochs_per_round != current.epochs_per_round:
        await _retire_rounds_for_interval_change(
            session,
            old=current.epochs_per_round,
            new=payload.settings.epochs_per_round,
            actor=actor,
            reason=reason,
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
    # Round slot reservations follow mode/validator_rounds_enabled; land the
    # change on this worker now, the others within the resolver TTL.
    invalidate_round_reservation(request.app.state)
    return _revision(row)


@router.get("/scorer", response_model=AdminGeneratorScorerStatus)
async def get_scorer_status(
    _admin: AdminDep, session: SessionDep
) -> AdminGeneratorScorerStatus:
    """What each generator scorer last advertised, and whether the live one
    can run the governing ``cases_per_seed``."""
    now = datetime.now(UTC)
    rows = await list_heartbeats(session)
    live = await live_scorer(session, now=now)
    cases = (await load_settings(session)).settings.cases_per_seed
    refused = refusal(live, cases)
    contract = await active_round_contract(session)
    known = (
        live is not None and live.min_cases is not None and live.max_cases is not None
    )
    return AdminGeneratorScorerStatus(
        scorers=[
            GeneratorScorerHeartbeatView(
                scorer_id=row.scorer_id,
                scorer_version=row.scorer_version,
                min_cases=row.min_cases,
                max_cases=row.max_cases,
                sandbox=row.sandbox,
                build_revision=row.build_revision,
                last_poll=row.last_poll,
                first_seen_at=row.first_seen_at,
                last_seen_at=row.last_seen_at,
                live=is_live(row, now=now),
                grading_bench_versions=row.grading_bench_versions,
                effective_grading_bench_versions=list(grading_versions(row)),
            )
            for row in rows
        ],
        live_scorer_id=live.scorer_id if live is not None else None,
        capability_ttl_seconds=int(SCORER_CAPABILITY_TTL.total_seconds()),
        cases_per_seed=cases,
        supports_cases_per_seed=(refused is None) if known else None,
        refusal=refused,
        round_contract=contract,
        supports_round_contract=(
            contract in grading_versions(live) if live is not None else None
        ),
    )


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
    rejected = row.status == "rejected"
    ruling = await _operator_ruling(session, row) if rejected else False
    blockers = (
        await _reinstate_blockers(session, row, override_operator_ruling=False)
        if rejected
        else []
    )
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
        operator_ruling=ruling,
        reinstate_blockers=blockers,
    )


async def _operator_ruling(session: AsyncSession, row: GeneratorSubmission) -> bool:
    """Whether the current rejection is an operator ruling.

    The audit log decides: the latest ``reject``/``reinstate`` action on the
    submission is a ``reject`` and the stored reason is still the one
    ``reject_submission`` stamped (a reinstated generator that a scorer verdict
    rejects again carries the verdict's reason instead). With no audit row,
    the ``operator (`` prefix alone decides.
    """
    latest = await session.scalar(
        select(GeneratorOperatorAction.action)
        .where(
            GeneratorOperatorAction.subject == str(row.submission_id),
            GeneratorOperatorAction.action.in_(("reject", "reinstate")),
        )
        .order_by(
            GeneratorOperatorAction.created_at.desc(),
            GeneratorOperatorAction.id.desc(),
        )
        .limit(1)
    )
    stamped = (row.status_reason or "").startswith("operator (")
    if latest == "reinstate":
        return False
    return stamped


async def _reinstate_blockers(
    session: AsyncSession,
    row: GeneratorSubmission,
    *,
    override_operator_ruling: bool,
) -> list[str]:
    """Every reason ``reinstate`` refuses this submission now.

    Shared by the write and the submission detail, so the operator sees the
    exact guard before acting. Uploads keep at most one active submission per
    hotkey, and a newer upload is the miner's choice; neither is undone here.
    """
    if row.status != "rejected":
        return [f"submission is {row.status}, not rejected"]
    blockers: list[str] = []
    if not override_operator_ruling and await _operator_ruling(session, row):
        blockers.append(
            "rejected by an operator ruling; pass override_operator_ruling to "
            "undo the ruling"
        )
    others = await session.execute(
        select(
            GeneratorSubmission.submission_id,
            GeneratorSubmission.status,
            GeneratorSubmission.submitted_at,
        )
        .where(
            GeneratorSubmission.miner_hotkey == row.miner_hotkey,
            GeneratorSubmission.submission_id != row.submission_id,
        )
        .order_by(GeneratorSubmission.submitted_at, GeneratorSubmission.submission_id)
    )
    for other_id, other_status, submitted_at in others.tuples():
        if other_status == "active":
            blockers.append(
                f"hotkey already has active submission {other_id}; one active "
                "generator per hotkey"
            )
        elif _aware(submitted_at) > _aware(row.submitted_at):
            blockers.append(
                f"hotkey uploaded newer submission {other_id} ({other_status}); "
                "a newer upload supersedes this one"
            )
    if row.public_default:
        other_default = await session.scalar(
            select(GeneratorSubmission.submission_id).where(
                GeneratorSubmission.public_default.is_(True),
                GeneratorSubmission.status == "active",
                GeneratorSubmission.submission_id != row.submission_id,
            )
        )
        if other_default is not None:
            blockers.append(
                f"public default generator {other_default} is already active"
            )
    return blockers


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


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
    """Re-qualify an active generator now on fresh seeds (build, validity,
    determinism, probe). It stays out of new round columns until it passes
    again; an in-flight qualification run is left to finish."""
    row, reason, actor = await _locked_submission(session, submission_id, payload)
    if row.status != "active":
        raise HTTPException(status_code=409, detail="only an active generator runs")
    now = datetime.now(UTC)
    row.next_run_at = now
    row.qualified_at = None
    row.attempts = 0
    row.updated_at = now
    record_operator_action(
        session,
        action="rescore",
        subject=str(row.submission_id),
        actor=actor,
        reason=reason,
    )
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

    It leaves the ledger at once, an in-flight qualification run is abandoned,
    and it never holds another round column. A column it holds in a round
    already generated keeps running (the dataset is frozen) but its score is
    never credited. The miner can still upload a new generator.
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
    record_operator_action(
        session,
        action="reject",
        subject=str(row.submission_id),
        actor=actor,
        reason=reason,
    )
    await session.commit()
    return await admin_submission_detail(submission_id, _admin, session)


@router.post(
    "/submissions/{submission_id}/reinstate",
    response_model=AdminGeneratorSubmissionDetail,
)
async def reinstate_submission(
    submission_id: UUID,
    payload: AdminGeneratorReinstateRequest,
    _admin: AdminDep,
    session: SessionDep,
) -> AdminGeneratorSubmissionDetail:
    """Return a rejected generator to ``active`` for a fresh qualification.

    For a rejection that was not the generator's fault (a scorer or
    infrastructure failure read as a generator verdict). The submission
    qualifies again from scratch on fresh secret seeds and holds no round
    column until it passes. Guarded by the exact status and status_reason the
    operator read. Refused while the hotkey has another active or a newer
    submission, and for an operator ruling unless ``override_operator_ruling``.
    """
    if payload.confirmation != REINSTATE_SUBMISSION_CONFIRMATION:
        raise HTTPException(
            status_code=409,
            detail=f"confirmation must be exactly {REINSTATE_SUBMISSION_CONFIRMATION}",
        )
    reason = payload.reason.strip()
    actor = payload.actor.strip()
    if len(reason) < MIN_REASON_LENGTH or not 1 <= len(actor) <= MAX_ACTOR_LENGTH:
        raise HTTPException(status_code=422, detail="reason and actor are required")
    hotkey = await session.scalar(
        select(GeneratorSubmission.miner_hotkey).where(
            GeneratorSubmission.submission_id == submission_id
        )
    )
    if hotkey is None:
        raise HTTPException(status_code=404, detail="unknown submission")
    # The upload path's per-hotkey lock: an upload racing this reinstate would
    # otherwise leave the hotkey with two active submissions.
    await lock_hotkey_uploads(session, hotkey)
    row = await get_submission(session, submission_id, for_update=True)
    if row is None:
        raise HTTPException(status_code=404, detail="unknown submission")
    status, status_reason = row.status, row.status_reason
    if status != payload.expected_status:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail=(
                f"submission is {status}, not {payload.expected_status}; "
                "re-read it and retry"
            ),
        )
    if status_reason != payload.expected_status_reason:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail=(
                "status_reason changed since it was read; re-read the submission "
                f"(current: {status_reason!r})"
            ),
        )
    blockers = await _reinstate_blockers(
        session, row, override_operator_ruling=payload.override_operator_ruling
    )
    if blockers:
        await session.rollback()
        raise HTTPException(status_code=409, detail="; ".join(blockers))
    ruling = await _operator_ruling(session, row)
    now = datetime.now(UTC)
    if row.lease_run_id is not None:
        await abandon_run(session, row.lease_run_id, now=now)
        clear_lease(row, now=now)
    previous_reason = row.status_reason
    row.status = "active"
    row.status_reason = None
    row.qualified_at = None
    row.attempts = 0
    row.next_run_at = now
    row.updated_at = now
    record_operator_action(
        session,
        action="reinstate",
        subject=str(row.submission_id),
        actor=actor,
        reason=reason,
        detail={
            "previous_status_reason": previous_reason,
            "operator_ruling": ruling,
            "override_operator_ruling": payload.override_operator_ruling,
        },
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail="submission changed concurrently; re-read it and retry",
        ) from exc
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
        # A leased run's seeds are live secrets, and an abandoned run's were
        # exposed to a scorer that never reported. Only a run that reported
        # reveals them, so an operator can reproduce the exact packs.
        seeds=list(run.seeds) if run.status not in ("leased", "abandoned") else None,
        probe_epoch=run.probe_epoch,
        cases_per_seed=run.cases_per_seed,
        settings_revision=run.settings_revision,
        scorer_version=run.scorer_version,
        panel=[],
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
    """Discriminating cases from scored rounds, highest credit first.

    The public default generator's cases are included: a default family that
    still splits the top agents is as useful to promote as a miner's.
    """
    rows = await session.execute(
        select(GeneratorRoundCaseVerdict, GeneratorRound, GeneratorSubmission)
        .join(
            GeneratorRound,
            GeneratorRound.round_index == GeneratorRoundCaseVerdict.round_index,
        )
        .join(
            GeneratorSubmission,
            GeneratorSubmission.submission_id
            == GeneratorRoundCaseVerdict.submission_id,
        )
        .where(
            GeneratorRoundCaseVerdict.status == "discriminating",
            GeneratorRound.status == "scored",
            GeneratorSubmission.status != "rejected",
        )
        .order_by(
            GeneratorRoundCaseVerdict.credit.desc(),
            GeneratorRound.round_index.desc(),
            GeneratorRoundCaseVerdict.column_id,
            GeneratorRoundCaseVerdict.case_id,
        )
        .limit(limit)
        .offset(offset)
    )
    return AdminGeneratorPromotionCandidatesResponse(
        candidates=[
            GeneratorPromotionCandidate(
                round_index=rnd.round_index,
                column_id=verdict.column_id,
                submission_id=sub.submission_id,
                miner_hotkey=sub.miner_hotkey,
                case_id=verdict.case_id,
                fingerprint=verdict.fingerprint,
                mean_score=verdict.mean_score,
                solver_owners=verdict.solver_owners,
                champion_failed=verdict.champion_failed,
                credit=verdict.credit,
                scored_at=rnd.scored_at,  # type: ignore[arg-type]
            )
            for verdict, rnd, sub in rows.tuples()
        ]
    )
