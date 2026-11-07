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
* **Fail-safe bounds.** ``target_policy_version`` must be at least the floor, at
  most the version this build implements, and no higher than the separately
  published activation ceiling. Shipping review code is not authority to
  activate an incomplete policy lifecycle.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, NoReturn, cast
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.screener_policy_activation import (
    RESTORE_SCORED_CONFIRMATION,
    RestoredScoredSubmission,
    RestoreScoredScreeningSnapshotRequest,
    RestoreScoredScreeningSnapshotResponse,
    ScoredPolicyRescreenReleaseView,
    ScoredPolicyRescreenView,
    ScoredRescreenState,
    ScreenerFleetPolicyReadinessView,
    ScreenerPolicyActivationRevision,
    ScreenerPolicyActivationView,
    V13ReviewClockRevision,
    V13ReviewClockSchedule,
)
from ditto.api_models.system_health import fleet_release_from_heartbeat_envelope
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
    ScreenerHeartbeat,
    ScreeningAttempt,
    ScreeningReviewDeadlineActivation,
)
from ditto.db.models import (
    ScreenerPolicyActivation as ActivationRow,
)
from ditto.db.queries.heartbeats import list_screener_heartbeats
from ditto.db.queries.scores import list_eligible_ledger
from ditto.db.queries.screener_policy_activation import (
    latest_screener_policy_activation,
    list_screener_policy_activations,
)
from ditto.db.queries.screening import prerequisite_screening_predicates
from ditto.db.queries.screening_review_deadlines import (
    FIRST_V13_CLAIM_EVENT,
    POLICY_V13_DOCUMENT_DIGEST,
)
from ditto_screening_protocol import (
    SCREENING_ACTIVATION_CEILING_POLICY_VERSION,
    SCREENING_FLOOR_POLICY_VERSION,
    SCREENING_POLICY_VERSION,
)

router = APIRouter(prefix="/admin/screener-policy-activation", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]


SOURCE_REVIEW_RETIRED_DETAIL = (
    "screening source review is retired (owner decision 2026-11-07): new "
    "review-policy activations, review clocks and scored-policy rescreens are no "
    "longer scheduled; build and safety checks are the whole screen"
)


_RETIRED_RESPONSES: dict[int | str, dict[str, Any]] = {
    410: {"description": SOURCE_REVIEW_RETIRED_DETAIL}
}


def _source_review_retired() -> HTTPException:
    return HTTPException(status_code=410, detail=SOURCE_REVIEW_RETIRED_DETAIL)


def _review_clock_revision(
    row: ScreeningReviewDeadlineActivation, *, now: datetime
) -> V13ReviewClockRevision:
    return V13ReviewClockRevision(
        revision=row.revision,
        policy_version=row.policy_version,
        policy_document_digest=row.policy_document_digest,
        policy_manifest_digest=row.policy_digest,
        activate_at=row.activate_at,
        window_seconds=row.window_seconds,
        start_event=FIRST_V13_CLAIM_EVENT,
        reason=row.reason,
        actor=row.actor,
        created_at=row.created_at,
        state="due" if row.activate_at <= now else "pending",
    )


async def _review_clock_schedule(session: AsyncSession) -> V13ReviewClockSchedule:
    rows = list(
        await session.scalars(
            select(ScreeningReviewDeadlineActivation)
            .order_by(ScreeningReviewDeadlineActivation.revision.desc())
            .limit(100)
        )
    )
    now = datetime.now(UTC)
    return V13ReviewClockSchedule(
        current_policy_document_digest=POLICY_V13_DOCUMENT_DIGEST,
        latest=_review_clock_revision(rows[0], now=now) if rows else None,
        revisions=[_review_clock_revision(row, now=now) for row in rows],
    )


@router.get("/review-clock", response_model=V13ReviewClockSchedule)
async def get_v13_review_clock(
    _admin: AdminDep, session: SessionDep
) -> V13ReviewClockSchedule:
    """Read the explicit future schedule; absence means no active cutoff."""
    return await _review_clock_schedule(session)


@router.post("/review-clock", status_code=410, responses=_RETIRED_RESPONSES)
async def schedule_v13_review_clock(
    _admin: AdminDep,
) -> NoReturn:
    """Retired: source review no longer runs, so no review clock is scheduled."""
    raise _source_review_retired()


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


_FLEET_FRESHNESS = timedelta(minutes=15)


async def _fleet_view(session: AsyncSession) -> ScreenerFleetPolicyReadinessView:
    """Summarize which policy versions the fresh reporting fleet can screen."""
    now = datetime.now(UTC)
    fresh: list[ScreenerHeartbeat] = []
    for row in await list_screener_heartbeats(session):
        seen_at = row.seen_at
        if seen_at.tzinfo is None:
            seen_at = seen_at.replace(tzinfo=UTC)
        if now - seen_at <= _FLEET_FRESHNESS:
            fresh.append(row)
    builtins: list[int] = []
    without_release: list[str] = []
    lagging: list[str] = []
    revisions: set[str] = set()
    versions: set[str] = set()
    for row in fresh:
        release = fleet_release_from_heartbeat_envelope(row.system_metrics)
        if release is None:
            without_release.append(row.instance_id)
            continue
        builtins.append(release.builtin_policy_version)
        if release.builtin_policy_version < SCREENING_POLICY_VERSION:
            lagging.append(row.instance_id)
        if release.revision is not None:
            revisions.add(release.revision)
        if release.version is not None:
            versions.add(release.version)
    return ScreenerFleetPolicyReadinessView(
        instances_reporting=len(fresh),
        instances_with_release=len(builtins),
        instances_without_release=sorted(without_release),
        min_builtin_policy_version=min(builtins) if builtins else None,
        max_builtin_policy_version=max(builtins) if builtins else None,
        # Every fresh worker must be able to claim the target, so the safe
        # ceiling is the smallest builtin; unknown builds make it unknown.
        safe_to_schedule_up_to=(
            min(min(builtins), SCREENING_ACTIVATION_CEILING_POLICY_VERSION)
            if builtins and not without_release
            else None
        ),
        lagging_instances=sorted(lagging),
        release_revisions=sorted(revisions),
        release_versions=sorted(versions),
    )


def _view(
    policy: EffectiveScreenerPolicy,
    latest: ActivationRow | None,
    history: list[ActivationRow],
    fleet: ScreenerFleetPolicyReadinessView | None = None,
) -> ScreenerPolicyActivationView:
    now = datetime.now(UTC)
    return ScreenerPolicyActivationView(
        effective_policy_version=policy.required_policy_version,
        floor_policy_version=SCREENING_FLOOR_POLICY_VERSION,
        builtin_policy_version=SCREENING_POLICY_VERSION,
        latest=_revision_view(latest, now=now) if latest is not None else None,
        revisions=[_revision_view(row, now=now) for row in history],
        fleet=fleet,
    )


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
    return _view(policy, latest, history, fleet=await _fleet_view(session))


@router.get("/scored-rescreen", response_model=ScoredPolicyRescreenView)
async def get_scored_rescreen(
    _admin: AdminDep, session: SessionDep
) -> ScoredPolicyRescreenView:
    """Read the bounded, score-preserving policy rollout checkpoint."""
    return await _scored_rescreen_view(
        session, policy=await resolve_screener_policy_activation(session)
    )


@router.post("/advance-scored-rescreen", status_code=410, responses=_RETIRED_RESPONSES)
async def advance_scored_rescreen(
    _admin: AdminDep,
) -> NoReturn:
    """Retired: scored agents are never rescreened for a review policy."""
    raise _source_review_retired()


@router.post("", status_code=410, responses=_RETIRED_RESPONSES)
async def schedule_activation(
    _admin: AdminDep,
) -> NoReturn:
    """Retired: the required screening policy is frozen at its last activation.

    Validators lease on the benchmark contract floor alone, so nothing waits on
    a new review-policy version. Reads of the activation history still work.
    """
    raise _source_review_retired()


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
