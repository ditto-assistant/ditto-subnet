"""Audited operator control for the required screening-policy version.

Screening policy text ships with the build, but raising the version the
screening queue REQUIRES is a fairness decision: every miner gets the same
notice period, and the activation time is public and scheduled rather than
"whenever an operator wakes up." This router stores the schedule; the queue,
claim, heartbeat, and outcome paths read the effective version from
``ditto.api_server.screener_policy_activation``.

Deliberately a separate router prefix from the screener protocol router: this
is subnet policy that Backroom may set over MCP, like the queue-policy and
source-review-settings boards.

Two safety rules live here rather than in the model, because both need state
the model cannot see:

* **Optimistic concurrency.** ``expected_revision`` must match the newest
  stored revision so two operators (or a stale Backroom tab) cannot schedule
  past each other; the write flushes inside the request transaction so the
  database's unique ``(parent_revision)`` constraint is the final arbiter.
* **Fail-safe bounds.** ``target_policy_version`` must be at least the floor and
  at most the version this build implements: scheduling a version no deployed
  worker implements would fail the whole screening fleet closed at activation
  time.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, cast
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.screener_policy_activation import (
    ADVANCE_SCORED_RESCREEN_CONFIRMATION,
    CONFIRMATION,
    RESTORE_SCORED_CONFIRMATION,
    AdvanceScoredPolicyRescreenRequest,
    RestoredScoredSubmission,
    RestoreScoredScreeningSnapshotRequest,
    RestoreScoredScreeningSnapshotResponse,
    ScheduleScreenerPolicyActivationRequest,
    ScoredPolicyRescreenReleaseView,
    ScoredPolicyRescreenView,
    ScoredRescreenState,
    ScreenerPolicyActivationRevision,
    ScreenerPolicyActivationView,
)
from ditto.api_models.screener_review_settings import ScreenerReviewSettings
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.api_server.screener_policy_activation import (
    EffectiveScreenerPolicy,
    resolve_screener_policy_activation,
)
from ditto.db.models import (
    Agent,
    Score,
    ScoredPolicyRescreenRelease,
    ScoredScreeningSnapshotRestoration,
    ScreenerReviewSettingsRevision,
    ScreeningAttempt,
)
from ditto.db.models import (
    ScreenerPolicyActivation as ActivationRow,
)
from ditto.db.queries.scores import list_eligible_ledger
from ditto.db.queries.screener_policy_activation import (
    insert_screener_policy_activation,
    latest_screener_policy_activation,
    list_screener_policy_activations,
)
from ditto.db.queries.screening import prerequisite_screening_predicates
from ditto_screening_protocol import (
    SCREENING_FLOOR_POLICY_VERSION,
    SCREENING_POLICY_VERSION,
)

router = APIRouter(prefix="/admin/screener-policy-activation", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]


_MAX_SCORED_POLICY_RESCREEN_WINDOW = 4


def _revision_view(
    row: ActivationRow, *, now: datetime
) -> ScreenerPolicyActivationRevision:
    return ScreenerPolicyActivationRevision(
        revision=row.revision,
        parent_revision=row.parent_revision,
        target_policy_version=row.target_policy_version,
        activate_at=row.activate_at,
        rescreen_scored=row.rescreen_scored,
        canary_only=row.canary_only,
        reason=row.reason,
        actor=row.actor,
        created_at=row.created_at,
        state="due" if row.activate_at <= now else "pending",
    )


def _view(
    policy: EffectiveScreenerPolicy,
    latest: ActivationRow | None,
    history: list[ActivationRow],
) -> ScreenerPolicyActivationView:
    now = datetime.now(UTC)
    return ScreenerPolicyActivationView(
        effective_policy_version=policy.required_policy_version,
        floor_policy_version=SCREENING_FLOOR_POLICY_VERSION,
        builtin_policy_version=SCREENING_POLICY_VERSION,
        latest=_revision_view(latest, now=now) if latest is not None else None,
        revisions=[_revision_view(row, now=now) for row in history],
    )


async def _latest_revision(session: AsyncSession) -> int:
    # Serialize schedules with incident snapshot restorations. Both operations
    # lock the newest append-only row before trusting its revision.
    latest = await latest_screener_policy_activation(session, for_update=True)
    return latest.revision if latest is not None else 0


def _release_view(
    release: ScoredPolicyRescreenRelease,
) -> ScoredPolicyRescreenReleaseView:
    return ScoredPolicyRescreenReleaseView(
        activation_revision=release.activation_revision,
        target_policy_version=release.target_policy_version,
        agent_id=release.agent_id,
        position=release.position,
        # The database constraint is the runtime authority; narrow its checked
        # value here so the response preserves the explicit wire contract.
        state=cast(ScoredRescreenState, release.state),
        attempt_id=release.attempt_id,
        review_settings_revision=release.review_settings_revision,
    )


async def _next_scored_rescreen_candidate(
    session: AsyncSession,
    *,
    activation: ActivationRow,
    released_agent_ids: set[UUID],
) -> tuple[UUID, int] | None:
    """Choose the next current-board row, never an arbitrary stale score.

    The first five owner-reduced public-ledger rows form the canary prefix, so
    one miner cannot occupy it.  The non-deduped ledger then finishes every
    remaining scored submission in the same descending order.
    This is called only from an operator checkpoint, never from the poll path.
    """
    board = await list_eligible_ledger(
        session, include_fingerprints=False, include_details=False
    )
    all_scored = await list_eligible_ledger(
        session,
        include_fingerprints=False,
        include_details=False,
        dedupe_owners=False,
    )
    ordered_ids = list(
        dict.fromkeys(
            [row.agent_id for row in board[:5]] + [row.agent_id for row in all_scored]
        )
    )
    if not ordered_ids:
        return None
    _missing_dataset, admitted = await prerequisite_screening_predicates(session)
    admitted_ids = set(
        await session.scalars(
            select(Agent.agent_id).where(
                Agent.agent_id.in_(ordered_ids),
                Agent.status.in_((AgentStatus.SCORED, AgentStatus.LIVE)),
                Agent.screening_policy_version < activation.target_policy_version,
                admitted,
            )
        )
    )
    for position, agent_id in enumerate(ordered_ids, start=1):
        if agent_id in admitted_ids and agent_id not in released_agent_ids:
            return agent_id, position
    return None


async def _scored_rescreen_view(
    session: AsyncSession,
    *,
    policy: EffectiveScreenerPolicy,
) -> ScoredPolicyRescreenView:
    if (
        policy.governing_revision is None
        or not policy.rescreen_scored
        or not policy.rescreen_stale_agents
    ):
        return ScoredPolicyRescreenView(
            activation_revision=None,
            target_policy_version=None,
            current=None,
            active=[],
            next_agent_id=None,
            next_position=None,
        )
    activation = await session.get(ActivationRow, policy.governing_revision)
    if activation is None:
        return ScoredPolicyRescreenView(
            activation_revision=None,
            target_policy_version=None,
            current=None,
            active=[],
            next_agent_id=None,
            next_position=None,
        )
    releases = list(
        await session.scalars(
            select(ScoredPolicyRescreenRelease)
            .where(
                ScoredPolicyRescreenRelease.activation_revision == activation.revision
            )
            .order_by(ScoredPolicyRescreenRelease.position)
        )
    )
    active = [row for row in releases if row.state != "terminal"]
    current = active[0] if active else None
    next_candidate = None
    # A non-verdict is an explicit stop condition. The existing active window
    # can settle, but no later board position may be released until the paused
    # row is retried or otherwise resolved by the operator.
    if not any(row.state == "paused" for row in active):
        next_candidate = await _next_scored_rescreen_candidate(
            session,
            activation=activation,
            released_agent_ids={row.agent_id for row in releases},
        )
    return ScoredPolicyRescreenView(
        activation_revision=activation.revision,
        target_policy_version=activation.target_policy_version,
        current=_release_view(current) if current is not None else None,
        active=[_release_view(row) for row in active],
        next_agent_id=next_candidate[0] if next_candidate is not None else None,
        next_position=next_candidate[1] if next_candidate is not None else None,
    )


@router.get("", response_model=ScreenerPolicyActivationView)
async def get_activation(
    _admin: AdminDep, session: SessionDep
) -> ScreenerPolicyActivationView:
    """The effective required version, the governing schedule, and history."""
    policy = await resolve_screener_policy_activation(session)
    latest = await latest_screener_policy_activation(session)
    history = list(await list_screener_policy_activations(session))
    return _view(policy, latest, history)


@router.get("/scored-rescreen", response_model=ScoredPolicyRescreenView)
async def get_scored_rescreen(
    _admin: AdminDep, session: SessionDep
) -> ScoredPolicyRescreenView:
    """Read the bounded, score-preserving policy rollout checkpoint."""
    return await _scored_rescreen_view(
        session, policy=await resolve_screener_policy_activation(session)
    )


@router.post("/advance-scored-rescreen", response_model=ScoredPolicyRescreenView)
async def advance_scored_rescreen(
    payload: AdvanceScoredPolicyRescreenRequest,
    _admin: AdminDep,
    session: SessionDep,
) -> ScoredPolicyRescreenView:
    """Fill a bounded current-board window after an explicit checkpoint.

    The policy schedule establishes the rule.  It does *not* bulk-requeue
    every existing score: a false V11 clear remains observable before more than
    the configured window of V10 scores is touched.  A non-verdict leaves its
    row paused, prevents the window from widening, and requires an explicit
    retry rather than silently advancing.
    """
    if payload.confirmation != ADVANCE_SCORED_RESCREEN_CONFIRMATION:
        raise HTTPException(
            status_code=409,
            detail=(
                f"confirmation must be exactly {ADVANCE_SCORED_RESCREEN_CONFIRMATION}"
            ),
        )
    if payload.max_active_releases > _MAX_SCORED_POLICY_RESCREEN_WINDOW:
        raise HTTPException(
            status_code=422,
            detail=(
                "max_active_releases exceeds the four-slot scored policy "
                "rescreen safety window"
            ),
        )
    actor = (payload.actor or "backroom").strip()
    async with session.begin():
        policy = await resolve_screener_policy_activation(session)
        if (
            policy.governing_revision is None
            or not policy.rescreen_scored
            or not policy.rescreen_stale_agents
            or policy.governing_revision != payload.expected_activation_revision
        ):
            raise HTTPException(
                status_code=409,
                detail="no due scored policy rescreen matches the expected activation",
            )
        activation = await session.get(
            ActivationRow, policy.governing_revision, with_for_update=True
        )
        assert activation is not None
        releases = list(
            await session.scalars(
                select(ScoredPolicyRescreenRelease)
                .where(
                    ScoredPolicyRescreenRelease.activation_revision
                    == activation.revision
                )
                .order_by(ScoredPolicyRescreenRelease.position)
                .with_for_update()
            )
        )
        active = [row for row in releases if row.state != "terminal"]
        paused = [row for row in active if row.state == "paused"]
        if paused:
            current = next(
                (row for row in paused if row.agent_id == payload.expected_agent_id),
                None,
            )
            if current is None:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "scored policy rescreen is paused; refresh and retry the "
                        f"paused row ({paused[0].agent_id})"
                    ),
                )
            if payload.retry_paused:
                if (
                    payload.review_settings_revision is not None
                    and payload.review_settings_revision
                    != current.review_settings_revision
                ):
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "paused scored policy rescreens retain their original "
                            "review settings revision"
                        ),
                    )
                current.state = "pending"
                current.attempt_id = None
                current.actor = actor
                current.reason = payload.reason.strip()
            else:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "scored policy rescreen is paused; use retry_paused only "
                        "for that non-verdict"
                    ),
                )
        else:
            review_settings_revision = payload.review_settings_revision
            if activation.canary_only:
                if review_settings_revision is None:
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            "canary-only scored policy rescreens require an "
                            "immutable L4-enforce review settings revision"
                        ),
                    )
                review_revision = await session.get(
                    ScreenerReviewSettingsRevision, review_settings_revision
                )
                if review_revision is None:
                    raise HTTPException(
                        status_code=422,
                        detail="review settings revision does not exist",
                    )
                review_settings = ScreenerReviewSettings.model_validate(
                    review_revision.settings
                )
                if (
                    review_settings.mode != "enforce"
                    or not review_settings.l3_enabled
                    or review_settings.adjudicator_mode != "enforce"
                ):
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            "canary-only scored policy rescreens require enforce "
                            "mode with L3 and the adjudicator enforced"
                        ),
                    )
                active_review_revisions = {
                    row.review_settings_revision for row in active
                }
                if active_review_revisions and active_review_revisions != {
                    review_settings_revision
                }:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "active scored policy rescreens retain their immutable "
                            "review settings revision"
                        ),
                    )
            elif review_settings_revision is not None:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "review settings revision is only valid for a canary-only "
                        "scored policy rescreen"
                    ),
                )
            slots = payload.max_active_releases - len(active)
            if slots <= 0:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "scored policy rescreen window is full; wait for a terminal "
                        "verdict before backfilling another slot"
                    ),
                )
            next_candidate = await _next_scored_rescreen_candidate(
                session,
                activation=activation,
                released_agent_ids={row.agent_id for row in releases},
            )
            if next_candidate is None:
                raise HTTPException(
                    status_code=409,
                    detail="no eligible stale scored submission remains to rescreen",
                )
            agent_id, _position = next_candidate
            if agent_id != payload.expected_agent_id:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "scored policy rescreen order changed; refresh before "
                        f"advancing (next {agent_id})"
                    ),
                )
            released_agent_ids = {row.agent_id for row in releases}
            # ``max_active_releases`` is a capacity window, not a batch size:
            # a later checkpoint backfills only the slots that reached a
            # terminal verdict. That keeps the four Hetzner workers busy while
            # never releasing a whole historical board into fallback capacity.
            for _ in range(slots):
                candidate = await _next_scored_rescreen_candidate(
                    session,
                    activation=activation,
                    released_agent_ids=released_agent_ids,
                )
                if candidate is None:
                    break
                candidate_agent_id, candidate_position = candidate
                session.add(
                    ScoredPolicyRescreenRelease(
                        release_id=uuid4(),
                        activation_revision=activation.revision,
                        target_policy_version=activation.target_policy_version,
                        agent_id=candidate_agent_id,
                        position=candidate_position,
                        state="pending",
                        attempt_id=None,
                        review_settings_revision=review_settings_revision,
                        actor=actor,
                        reason=payload.reason.strip(),
                    )
                )
                released_agent_ids.add(candidate_agent_id)
    return await _scored_rescreen_view(
        session, policy=await resolve_screener_policy_activation(session)
    )


@router.post("", response_model=ScreenerPolicyActivationView)
async def schedule_activation(
    payload: ScheduleScreenerPolicyActivationRequest,
    request: Request,
    _admin: AdminDep,
    session: SessionDep,
) -> ScreenerPolicyActivationView:
    """Append one optimistic, confirmation-gated schedule revision.

    The required version rises to ``target_policy_version`` when
    ``activate_at`` passes, not when this row is written — miners get the full
    notice window. Superseding an earlier schedule is a normal append: the
    newest due revision governs.
    """
    if payload.confirmation != CONFIRMATION:
        raise HTTPException(
            status_code=409,
            detail=f"confirmation must be exactly {CONFIRMATION}",
        )
    if payload.activate_at.tzinfo is None:
        raise HTTPException(
            status_code=422,
            detail=(
                "activate_at must carry a timezone offset (for example "
                "2026-08-29T09:00:00-04:00 for 9 a.m. Eastern); a naive "
                "datetime would silently mean server-local time"
            ),
        )
    activate_at = payload.activate_at.astimezone(UTC)
    if activate_at <= datetime.now(UTC):
        raise HTTPException(
            status_code=422,
            detail=(
                "activate_at must be in the future; an activation is "
                "notice, not a retroactive rule change"
            ),
        )
    if payload.target_policy_version < SCREENING_FLOOR_POLICY_VERSION:
        raise HTTPException(
            status_code=422,
            detail=(
                f"target_policy_version must be at least the current floor "
                f"{SCREENING_FLOOR_POLICY_VERSION}"
            ),
        )
    if payload.target_policy_version > SCREENING_POLICY_VERSION:
        raise HTTPException(
            status_code=422,
            detail=(
                f"target_policy_version {payload.target_policy_version} exceeds "
                f"the version this build implements "
                f"({SCREENING_POLICY_VERSION}); deploy the build that "
                "implements it, then schedule the activation"
            ),
        )
    if payload.canary_only and not payload.rescreen_scored:
        raise HTTPException(
            status_code=422,
            detail="canary-only activation requires rescreen_scored=true",
        )
    if (
        payload.canary_only
        and payload.target_policy_version <= SCREENING_FLOOR_POLICY_VERSION
    ):
        raise HTTPException(
            status_code=422,
            detail="canary-only activation must target a policy above the floor",
        )
    actual_revision = await _latest_revision(session)
    if payload.expected_revision != actual_revision:
        raise HTTPException(
            status_code=409,
            detail=(
                "screener policy activation changed; refresh before applying "
                f"(expected {payload.expected_revision}, current {actual_revision})"
            ),
        )
    actor = (payload.actor or "backroom").strip()
    try:
        await insert_screener_policy_activation(
            session,
            parent_revision=actual_revision,
            target_policy_version=payload.target_policy_version,
            activate_at=activate_at,
            rescreen_scored=payload.rescreen_scored,
            canary_only=payload.canary_only,
            reason=payload.reason.strip(),
            actor=actor,
        )
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail="screener policy activation changed concurrently; refresh and retry",
        ) from error
    resolver = getattr(request.app.state, "screener_policy_activation", None)
    if resolver is not None:
        resolver.invalidate()
    policy = await resolve_screener_policy_activation(session)
    latest = await latest_screener_policy_activation(session)
    history = list(await list_screener_policy_activations(session))
    return _view(policy, latest, history)


@router.post(
    "/restore-scored-snapshot",
    response_model=RestoreScoredScreeningSnapshotResponse,
)
async def restore_scored_snapshot(
    payload: RestoreScoredScreeningSnapshotRequest,
    _admin: AdminDep,
    session: SessionDep,
) -> RestoreScoredScreeningSnapshotResponse:
    """Atomically restore the pre-activation pass for a displaced scored cohort.

    The cohort is derived under row locks from immutable score and screening
    history. Nothing is requeued and no historical attempt is changed.
    """
    if payload.confirmation != RESTORE_SCORED_CONFIRMATION:
        raise HTTPException(
            status_code=409,
            detail=f"confirmation must be exactly {RESTORE_SCORED_CONFIRMATION}",
        )
    if payload.source_policy_version <= payload.target_policy_version:
        raise HTTPException(
            status_code=422,
            detail="source_policy_version must be greater than target_policy_version",
        )

    actor = (payload.actor or "backroom").strip()
    batch_id = uuid4()
    restored: list[RestoredScoredSubmission] = []
    async with session.begin():
        current = await session.scalar(
            select(ActivationRow)
            .order_by(ActivationRow.revision.desc())
            .limit(1)
            .with_for_update()
        )
        if (
            current is None
            or current.revision != payload.expected_current_activation_revision
        ):
            actual = current.revision if current is not None else 0
            raise HTTPException(
                status_code=409,
                detail=(
                    "screener policy activation changed; refresh before restoring "
                    f"(expected {payload.expected_current_activation_revision}, "
                    f"current {actual})"
                ),
            )
        policy = await resolve_screener_policy_activation(session)
        if (
            policy.required_policy_version != payload.target_policy_version
            or current.target_policy_version != payload.target_policy_version
            or current.rescreen_scored
            or current.activate_at > datetime.now(UTC)
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    "current activation must already require the target policy "
                    "with scored rescreening disabled"
                ),
            )
        source = await session.scalar(
            select(ActivationRow)
            .where(ActivationRow.revision == payload.source_activation_revision)
            .with_for_update()
        )
        if (
            source is None
            or source.target_policy_version != payload.source_policy_version
            or not source.rescreen_scored
            or source.activate_at > datetime.now(UTC)
            or source.revision >= current.revision
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    "source activation is not an earlier due scored-rescreen "
                    "revision for the supplied source policy"
                ),
            )

        displaced = aliased(ScreeningAttempt)
        prior = aliased(ScreeningAttempt)
        score_counts = (
            select(
                Score.agent_id.label("agent_id"),
                func.count().label("score_count"),
            )
            .where(Score.bench_version == payload.bench_version)
            .group_by(Score.agent_id)
            .subquery()
        )
        latest_attempt_id = (
            select(ScreeningAttempt.attempt_id)
            .where(ScreeningAttempt.agent_id == Agent.agent_id)
            .order_by(
                ScreeningAttempt.started_at.desc(),
                ScreeningAttempt.attempt_id.desc(),
            )
            .limit(1)
            .correlate(Agent)
            .scalar_subquery()
        )
        prior_pass_id = (
            select(ScreeningAttempt.attempt_id)
            .where(
                ScreeningAttempt.agent_id == Agent.agent_id,
                ScreeningAttempt.status == "passed",
                ScreeningAttempt.policy_version <= payload.target_policy_version,
                ScreeningAttempt.started_at < source.activate_at,
            )
            .order_by(
                ScreeningAttempt.finished_at.desc().nullslast(),
                ScreeningAttempt.started_at.desc(),
                ScreeningAttempt.attempt_id.desc(),
            )
            .limit(1)
            .correlate(Agent)
            .scalar_subquery()
        )
        rows = (
            await session.execute(
                select(Agent, displaced, prior, score_counts.c.score_count)
                .join(score_counts, score_counts.c.agent_id == Agent.agent_id)
                .join(displaced, displaced.attempt_id == latest_attempt_id)
                .join(prior, prior.attempt_id == prior_pass_id)
                .where(
                    score_counts.c.score_count >= 3,
                    Agent.screening_policy_version == payload.source_policy_version,
                    displaced.policy_version == payload.source_policy_version,
                    displaced.started_at >= source.activate_at,
                    displaced.status.in_(("failed", "rejected")),
                    or_(
                        and_(
                            Agent.status == AgentStatus.SCREENING_FAILED,
                            displaced.status == "failed",
                        ),
                        and_(
                            Agent.status == AgentStatus.REJECTED,
                            displaced.status == "rejected",
                        ),
                    ),
                    ~exists().where(
                        ScoredScreeningSnapshotRestoration.displaced_attempt_id
                        == displaced.attempt_id
                    ),
                )
                .order_by(Agent.agent_id)
                .with_for_update(of=Agent)
            )
        ).all()
        if len(rows) != payload.expected_count:
            raise HTTPException(
                status_code=409,
                detail=(
                    "restoration cohort changed; refresh before restoring "
                    f"(expected {payload.expected_count}, current {len(rows)})"
                ),
            )

        for agent, displaced_attempt, restored_attempt, score_count in rows:
            previous_status = agent.status.value
            previous_policy_version = agent.screening_policy_version
            agent.status = AgentStatus.SCORED
            agent.screening_policy_version = restored_attempt.policy_version
            agent.screening_reason = None
            agent.screening_reason_code = None
            agent.duplicate_of = None
            session.add(
                ScoredScreeningSnapshotRestoration(
                    restoration_id=uuid4(),
                    batch_id=batch_id,
                    agent_id=agent.agent_id,
                    displaced_attempt_id=displaced_attempt.attempt_id,
                    restored_attempt_id=restored_attempt.attempt_id,
                    source_activation_revision=source.revision,
                    current_activation_revision=current.revision,
                    source_policy_version=payload.source_policy_version,
                    target_policy_version=payload.target_policy_version,
                    bench_version=payload.bench_version,
                    previous_status=previous_status,
                    previous_policy_version=previous_policy_version,
                    restored_policy_version=restored_attempt.policy_version,
                    score_count=int(score_count),
                    actor=actor,
                    reason=payload.reason.strip(),
                )
            )
            restored.append(
                RestoredScoredSubmission(
                    agent_id=str(agent.agent_id),
                    displaced_attempt_id=str(displaced_attempt.attempt_id),
                    restored_attempt_id=str(restored_attempt.attempt_id),
                    restored_policy_version=restored_attempt.policy_version,
                    score_count=int(score_count),
                )
            )

    return RestoreScoredScreeningSnapshotResponse(
        batch_id=str(batch_id),
        restored_count=len(restored),
        source_activation_revision=payload.source_activation_revision,
        current_activation_revision=payload.expected_current_activation_revision,
        source_policy_version=payload.source_policy_version,
        target_policy_version=payload.target_policy_version,
        bench_version=payload.bench_version,
        submissions=restored,
    )
