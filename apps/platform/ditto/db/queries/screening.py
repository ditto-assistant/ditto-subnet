"""Leased screening attempts and their append-only public history."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import ColumnElement, and_, case, exists, func, or_, select
from sqlalchemy.orm import aliased
from sqlalchemy.sql.selectable import ScalarSelect

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.benchmark_contract import benchmark_contracts
from ditto.api_models.screener import SCREENING_POLICY_VERSION
from ditto.db.models import (
    Agent,
    AthReview,
    BenchmarkDataset,
    BenchmarkRollout,
    BenchmarkRolloutMember,
    EvaluationPayment,
    OwnerAttestation,
    Score,
    ScreenerHeartbeat,
    ScreeningAttempt,
    ScreeningQuarantine,
    ScreeningRetryOverride,
    SubmissionImageBuild,
)
from ditto.db.queries.benchmark_admission import (
    activated_rollout_for_version,
    benchmark_admission_predicate,
    validator_queue_admission_predicate,
)
from ditto.db.queries.benchmark_rollout import active_bench_version, open_rollout
from ditto.db.queries.scores import SCORING_QUORUM

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

# Expired attempts under the current policy after which an agent is parked for
# operator review instead of re-queued forever. An inconclusive screen is
# completed as an early-expired lease and remains in backoff until its original
# deadline; legacy workers still express the same state by letting the running
# lease expire naturally. A permanently-inconclusive agent would otherwise
# re-attempt every lease indefinitely. Only "expired" attempts count --
# infrastructure "failed" attempts are usually screener-side, so a screener
# outage must not mass-park every in-flight agent. Provider-routing failures
# stay in backoff until that original deadline so a Targon/Cloud Run blip
# cannot re-lease every few minutes. Heartbeat-proven orphans stay immediate.
MAX_SCREENING_EXPIRIES = 5

# Duplicate-owner statuses. A later cross-miner submission of the SAME bytes is
# flagged against the earliest owner in either set.
#
# "Usable" owners are live work being copied. "Adjudicated-negative" owners were
# refused, and only count when the refusal was for cause (see refused_for_cause
# in claim_screening_attempts) -- a build failure or an infrastructure reject
# says nothing about the artifact's provenance, so it must not condemn a later
# identical submission.
#
# In-flight statuses (UPLOADED / SCREENING / SCREENING_FAILED) belong to neither
# set: an owner still being screened is handled by deferring the claim
# (earlier_pending), not by flagging, so the race resolves before either is
# judged.
_USABLE_OWNER_STATUSES = (
    AgentStatus.EVALUATING,
    AgentStatus.SCORED,
    AgentStatus.LIVE,
    AgentStatus.ATH_PENDING_REVIEW,
)
_ADJUDICATED_NEGATIVE_OWNER_STATUSES = (
    AgentStatus.REJECTED,
    AgentStatus.QUARANTINED,
    AgentStatus.BANNED,
)

# A platform-raised quarantine has no screener finding, but the row's
# manifest_digest is NOT NULL and shown verbatim in the operator console. This
# stable sentinel marks the origin as "platform, attempts exhausted".
_EXHAUSTED_REASON_CODE = "repeatedly-inconclusive"
_EXHAUSTED_PUBLIC_REASON = (
    "Screening was inconclusive repeatedly; held for operator review"
)
_DEFERRED_MECHANICAL_REASON = "deferred-mechanical-admission"
_ORPHANED_ATTEMPT_REASON_CODE = "worker-lease-orphaned"
_ORPHANED_ATTEMPT_REASON = (
    "Screening worker stopped reporting this attempt; retry scheduled"
)
PROVIDER_BACKOFF_REASON_CODES = (
    "targon-build-unavailable",
    "targon-runtime-unavailable",
    "targon-source-review-unavailable",
    "cloudrun-build-unavailable",
    "cloudrun-runtime-unavailable",
)
# Active workers report at least every two minutes. Wait through two complete
# heartbeat intervals before inferring an orphan, and only act on heartbeat
# observations fresh enough to classify that worker as online publicly.
_ORPHANED_ATTEMPT_GRACE = timedelta(minutes=5)
_SCREENER_HEARTBEAT_FRESHNESS = timedelta(minutes=5)
_EXHAUSTED_MANIFEST_DIGEST = hashlib.sha256(
    b"ditto:repeatedly-inconclusive:v1"
).hexdigest()


def screening_score_count() -> ScalarSelect[int]:
    """Return the accepted-score count correlated to the current agent."""
    return (
        select(func.count())
        .where(Score.agent_id == Agent.agent_id)
        .correlate(Agent)
        .scalar_subquery()
    )


def screening_last_served_at() -> ColumnElement[Any]:
    """Return when current-policy screening last consumed a queue turn."""
    latest_attempt = (
        select(
            func.max(
                func.coalesce(
                    ScreeningAttempt.finished_at,
                    ScreeningAttempt.deadline,
                    ScreeningAttempt.started_at,
                )
            )
        )
        .where(
            ScreeningAttempt.agent_id == Agent.agent_id,
            ScreeningAttempt.policy_version == SCREENING_POLICY_VERSION,
        )
        .correlate(Agent)
        .scalar_subquery()
    )
    return func.coalesce(latest_attempt, Agent.created_at)


def screening_priority_order() -> tuple[ColumnElement[Any], ...]:
    """Prioritize finalists while interleaving bounded screening retries.

    A policy bump can return the whole scored field to screening. Submissions
    already one score from quorum should not lose their chance to finalize
    behind the rescreen backlog. Within each lane, the least recently served
    submission goes first: an expired lease moves an item behind the untouched
    backlog, but it remains ahead of submissions arriving later. This prevents
    either retries or fresh arrivals from monopolizing the worker while
    preserving the existing score and age tie-breakers.
    """
    score_count = screening_score_count()
    last_served_at = screening_last_served_at()
    provisional_composite = (
        select(func.avg(Score.composite))
        .where(Score.agent_id == Agent.agent_id)
        .correlate(Agent)
        .scalar_subquery()
    )
    in_completion_lane = case(
        (score_count >= SCORING_QUORUM - 1, 1),
        else_=0,
    )
    completion_lane_score = case(
        (score_count >= SCORING_QUORUM - 1, provisional_composite),
        else_=0.0,
    )
    return (
        in_completion_lane.desc(),
        completion_lane_score.desc(),
        score_count.asc(),
        last_served_at.asc(),
        Agent.created_at.asc(),
        Agent.agent_id.asc(),
    )


async def prerequisite_screening_predicates(
    session: AsyncSession,
) -> tuple[ColumnElement[bool], ColumnElement[bool]]:
    """Return missing-prerequisite and validator-admission predicates.

    Effective benchmark authority can move to an open rollout's desired version
    before that rollout's durable row becomes ``activated``. New submissions
    created during an open rollout also enter its desired version immediately,
    even before the authority guard flips. Mirror those two rules here instead
    of treating the most recent literal ``activated`` row as current authority.

    Current-policy ``EVALUATING`` agents only return to screening to rebuild a
    missing benchmark dataset or screened image. Apply the same benchmark-era
    admission boundary as the validator allocator before spending screener
    capacity on that rebuild. In particular, merely generating a newer dataset
    must never self-admit a historical submission.

    While a rollout is open, the persisted version and desired version can both
    accept work: existing agents remain on the former while new arrivals and
    explicit rollout admissions enter the latter. Once effective authority has
    flipped to the desired version, only that rollout's boundary remains valid.
    """
    effective_version = await active_bench_version(session)
    rollout = await open_rollout(session)
    required_version: int | ColumnElement[int] = effective_version
    if rollout is not None:
        required_version = case(
            (Agent.created_at >= rollout.created_at, rollout.desired_version),
            else_=effective_version,
        )
    activation_exists = exists(
        select(BenchmarkRollout.rollout_id).where(
            BenchmarkRollout.status == "activated"
        )
    )
    versioned_dataset_exists = exists(
        select(BenchmarkDataset.agent_id).where(
            BenchmarkDataset.agent_id == Agent.agent_id,
            BenchmarkDataset.bench_version == required_version,
        )
    )
    missing_dataset = activation_exists & ~versioned_dataset_exists

    def admitted_for(
        *, bench_version: int, authority: BenchmarkRollout | None
    ) -> ColumnElement[bool]:
        admitted = validator_queue_admission_predicate(bench_version=bench_version)
        if authority is not None:
            admitted &= benchmark_admission_predicate(
                rollout=authority,
                bench_version=bench_version,
            )
        return admitted

    if rollout is not None and effective_version == rollout.desired_version:
        return (
            missing_dataset,
            admitted_for(
                bench_version=rollout.desired_version,
                authority=rollout,
            ),
        )

    active_rollout = await activated_rollout_for_version(
        session,
        bench_version=effective_version,
    )
    active_admission = admitted_for(
        bench_version=effective_version,
        authority=active_rollout,
    )
    if rollout is None:
        return missing_dataset, active_admission
    return (
        missing_dataset,
        or_(
            and_(
                Agent.created_at >= rollout.created_at,
                admitted_for(
                    bench_version=rollout.desired_version,
                    authority=rollout,
                ),
            ),
            and_(
                Agent.created_at < rollout.created_at,
                or_(
                    active_admission,
                    admitted_for(
                        bench_version=rollout.desired_version,
                        authority=rollout,
                    ),
                ),
            ),
        ),
    )


def missing_active_screened_image() -> ColumnElement[bool]:
    """Whether the current agent lacks a complete screened image the activated
    benchmark version requires (v3+).

    Validators only lease an agent for a screened-image benchmark once every
    ``screened_image_*`` field is set (see ``eligible_screened_image`` in
    ``db/queries/tickets.py``). An agent quarantined mid-screen — before its
    image was uploaded and verified — and then RELEASED to ``evaluating`` on the
    current policy has an incomplete image: validators skip it, and without this
    predicate no screener re-claims it, so it is stuck for good. Re-screening it
    rebuilds and verifies the image (and its dataset)."""
    active_version = (
        select(BenchmarkRollout.desired_version)
        .where(BenchmarkRollout.status == "activated")
        .order_by(BenchmarkRollout.activated_at.desc())
        .limit(1)
        .scalar_subquery()
    )
    screened_versions = [
        contract.version
        for contract in benchmark_contracts()
        if contract.requires_screened_image
    ]
    incomplete_image = (
        Agent.screened_image_sha256.is_(None)
        | Agent.screened_image_size_bytes.is_(None)
        | Agent.screened_image_id.is_(None)
        | Agent.screened_image_ref.is_(None)
        | Agent.screened_image_upload_id.is_(None)
        | Agent.screened_image_verified_at.is_(None)
    )
    return active_version.in_(screened_versions) & incomplete_image


async def expire_screening_attempts(session: AsyncSession, *, now: datetime) -> int:
    """Expire overdue leases and return their submissions to the retry pool."""
    attempts = list(
        await session.scalars(
            select(ScreeningAttempt)
            .where(
                ScreeningAttempt.status == "running",
                ScreeningAttempt.deadline < now,
            )
            .with_for_update()
        )
    )
    for attempt in attempts:
        attempt.status = "expired"
        attempt.finished_at = now
        attempt.public_reason = "Screening lease expired"
        agent = await session.get(Agent, attempt.agent_id)
        if agent is not None and agent.status == AgentStatus.SCREENING:
            agent.status = AgentStatus.SCREENING_FAILED
            agent.screening_reason = "Screening lease expired"
    return len(attempts)


async def fail_orphaned_screening_attempts(
    session: AsyncSession,
    *,
    screener_hotkey: str,
    now: datetime,
) -> int:
    """Release leases that no live worker still reports as active.

    Verdict delivery is idempotent, but a transient failure after a completed
    build used to leave the attempt ``running`` for its full 70-minute TTL. The
    shared screener hotkey cannot identify the claiming instance directly, so
    this uses only positive fleet evidence: the attempt is past a two-heartbeat
    grace, at least one fresh heartbeat was observed after it started, and no
    fresh instance reports that agent as its active work.

    Platform-attested Targon screens do not heartbeat. A leftover GCE pet
    (``ditto-screener-prod``) that is still polling would otherwise orphan
    those Kaniko leases every five minutes. An in-flight submission image
    build is positive evidence the attempt is still being worked.

    These are infrastructure failures, not inconclusive reviews. Mark them
    ``failed`` so they retry immediately without consuming the five-expiry
    adjudication budget.
    """
    attempts = list(
        await session.scalars(
            select(ScreeningAttempt)
            .where(
                ScreeningAttempt.screener_hotkey == screener_hotkey,
                ScreeningAttempt.status == "running",
                ScreeningAttempt.started_at <= now - _ORPHANED_ATTEMPT_GRACE,
                ScreeningAttempt.deadline > now,
            )
            .with_for_update()
        )
    )
    if not attempts:
        return 0
    heartbeats = list(
        await session.scalars(
            select(ScreenerHeartbeat).where(
                ScreenerHeartbeat.screener_hotkey == screener_hotkey,
                ScreenerHeartbeat.seen_at >= now - _SCREENER_HEARTBEAT_FRESHNESS,
            )
        )
    )
    if not heartbeats:
        return 0

    in_flight_builds = set(
        await session.scalars(
            select(SubmissionImageBuild.attempt_id).where(
                SubmissionImageBuild.attempt_id.in_(
                    [attempt.attempt_id for attempt in attempts]
                ),
                SubmissionImageBuild.status.in_(
                    ("queued", "leased", "running", "succeeded")
                ),
            )
        )
    )

    failed = 0
    for attempt in attempts:
        started_at = attempt.started_at
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=UTC)
        observed_after_claim = any(
            (
                heartbeat.seen_at
                if heartbeat.seen_at.tzinfo is not None
                else heartbeat.seen_at.replace(tzinfo=UTC)
            )
            > started_at
            for heartbeat in heartbeats
        )
        still_active = any(
            heartbeat.state == "screening"
            and heartbeat.active_agent_id == attempt.agent_id
            for heartbeat in heartbeats
        )
        if (
            not observed_after_claim
            or still_active
            or attempt.attempt_id in in_flight_builds
        ):
            continue
        attempt.status = "failed"
        attempt.finished_at = now
        attempt.public_reason = _ORPHANED_ATTEMPT_REASON
        attempt.reason_code = _ORPHANED_ATTEMPT_REASON_CODE
        agent = await session.get(Agent, attempt.agent_id)
        if agent is not None and agent.status == AgentStatus.SCREENING:
            agent.status = AgentStatus.SCREENING_FAILED
            agent.screening_reason = _ORPHANED_ATTEMPT_REASON
            agent.screening_reason_code = _ORPHANED_ATTEMPT_REASON_CODE
        failed += 1
    return failed


async def _fresh_heartbeat_instance_count(
    session: AsyncSession,
    *,
    screener_hotkey: str,
    now: datetime,
) -> int:
    """Count live fleet instances for one shared screener hotkey."""
    count = await session.scalar(
        select(func.count())
        .select_from(ScreenerHeartbeat)
        .where(
            ScreenerHeartbeat.screener_hotkey == screener_hotkey,
            ScreenerHeartbeat.seen_at >= now - _SCREENER_HEARTBEAT_FRESHNESS,
        )
    )
    return int(count or 0)


async def _running_attempt_count_for_hotkey(
    session: AsyncSession,
    *,
    screener_hotkey: str,
) -> int:
    """Count in-flight screening leases held by one shared screener hotkey."""
    count = await session.scalar(
        select(func.count())
        .select_from(ScreeningAttempt)
        .where(
            ScreeningAttempt.screener_hotkey == screener_hotkey,
            ScreeningAttempt.status == "running",
        )
    )
    return int(count or 0)


async def _shared_hotkey_claim_budget(
    session: AsyncSession,
    *,
    screener_hotkey: str,
    now: datetime,
) -> int | None:
    """Return extra leases this hotkey may take, or ``None`` when uncapped.

    Claims are authenticated by the shared fleet hotkey; heartbeats are per
    ``instance_id``. Platform-attested Targon screens also claim on this
    hotkey and do not heartbeat. While a leftover GCE pet still reports,
    extra ``/claim``s from nested-Docker leftovers stack Kaniko leases that
    the pet then orphans every five minutes. Cap concurrent running attempts
    to the live instance count whenever at least one instance is heartbeating.
    No heartbeat yet keeps the historical uncapped path so the Platform
    Targon loop can admit work after ``ditto-screener-prod`` is deleted.
    """
    fresh_instances = await _fresh_heartbeat_instance_count(
        session,
        screener_hotkey=screener_hotkey,
        now=now,
    )
    if fresh_instances <= 0:
        return None
    running = await _running_attempt_count_for_hotkey(
        session,
        screener_hotkey=screener_hotkey,
    )
    return max(0, fresh_instances - running)


async def _expired_attempt_count(session: AsyncSession, *, agent_id: UUID) -> int:
    """Count expired screening leases under the current policy **since the
    agent's most recent operator clear**.

    Resolving a quarantine with ``release`` or ``rescreen`` explicitly lets the
    submission move forward and therefore grants a fresh attempt budget.
    Without the lower bound, an agent whose expiries came from a screener-fleet
    outage carries them forever: its next claim is instantly re-parked as
    ``repeatedly-inconclusive`` (started_at == finished_at, no screening ever
    runs). This first affected rescreens on 2026-07-16 and later released agents
    that needed a build-only pass for a missing screened image.
    """
    last_operator_clear = (
        select(func.max(ScreeningQuarantine.resolved_at))
        .where(
            ScreeningQuarantine.agent_id == agent_id,
            ScreeningQuarantine.resolution.in_(("release", "rescreen")),
        )
        .scalar_subquery()
    )
    count = await session.scalar(
        select(func.count())
        .select_from(ScreeningAttempt)
        .where(
            ScreeningAttempt.agent_id == agent_id,
            ScreeningAttempt.policy_version == SCREENING_POLICY_VERSION,
            ScreeningAttempt.status == "expired",
            ScreeningAttempt.started_at
            > func.coalesce(last_operator_clear, datetime(1970, 1, 1, tzinfo=UTC)),
        )
    )
    return int(count or 0)


async def _park_repeatedly_inconclusive(
    session: AsyncSession,
    agent: Agent,
    *,
    screener_hotkey: str,
    now: datetime,
) -> None:
    """Quarantine an agent that keeps expiring its lease, for operator review.

    Records a terminal ``quarantined`` attempt plus an active quarantine row
    (the operator console is driven entirely by ``ScreeningQuarantine``) so the
    agent leaves the retry pool and a human decides its fate instead of the
    screener re-attempting it every lease forever.
    """
    attempt = ScreeningAttempt(
        attempt_id=uuid4(),
        agent_id=agent.agent_id,
        screener_hotkey=screener_hotkey,
        policy_version=SCREENING_POLICY_VERSION,
        status="quarantined",
        started_at=now,
        deadline=now,
        finished_at=now,
        public_reason=_EXHAUSTED_PUBLIC_REASON,
        reason_code=_EXHAUSTED_REASON_CODE,
    )
    session.add(attempt)
    # Flush so the attempt row exists before the quarantine's FK references it
    # (no ORM relationship links them to order the inserts automatically).
    await session.flush()
    session.add(
        ScreeningQuarantine(
            quarantine_id=uuid4(),
            agent_id=agent.agent_id,
            attempt_id=attempt.attempt_id,
            screener_hotkey=screener_hotkey,
            policy_version=SCREENING_POLICY_VERSION,
            manifest_digest=_EXHAUSTED_MANIFEST_DIGEST,
            finding_digest=None,
            reason_code=_EXHAUSTED_REASON_CODE,
            evidence=None,
            finding=None,
            status="active",
        )
    )
    agent.status = AgentStatus.QUARANTINED
    agent.screening_reason = _EXHAUSTED_PUBLIC_REASON
    agent.screening_reason_code = _EXHAUSTED_REASON_CODE


async def claim_screening_attempts(
    session: AsyncSession,
    *,
    screener_hotkey: str,
    now: datetime,
    ttl: timedelta,
    limit: int,
    netuid: int = 118,
    deferred_review_mode: str = "off",
) -> list[tuple[Agent, ScreeningAttempt, UUID | None]]:
    """Claim completion-lane contenders, then least-scored eligible work.

    ``deferred_review_mode`` is the operator's
    ``queue_policy_settings.deferred_source_review.mode``. It decides only how
    deep a *fresh* admission is screened: ``enforce`` and ``bypass`` admit on a
    cheap build-only pass, ``off`` and ``observe`` run the full deep screen
    before the submission is scoreable. It never decides whether an agent
    already holding a pending deferred review can be re-claimed for that
    review -- that stays eligible in every mode, or a mode change would strand
    the holds open when it was made.

    When at least one instance is heartbeating this shared hotkey, concurrent
    running leases cannot exceed that live instance count. That keeps
    leftover GCE pets from stacking and orphaning Targon-first Kaniko builds
    every five minutes. After those pets are gone, zero heartbeats leaves
    this path uncapped so Platform can admit Targon one-shot rentals.
    """
    # Claiming is already a short transaction. Serialize it in Postgres so two
    # workers cannot skip-lock sibling rows with the same hash and admit both.
    # SQLite serializes writes itself and does not provide advisory locks.
    bind = session.get_bind()
    if bind.dialect.name == "postgresql":
        await session.execute(select(func.pg_advisory_xact_lock(0x445554544F534352)))
    # A REJECTED agent is deliberately absent above. It re-enters screening only
    # through the operator appeal (POST /screening-submissions/{id}/rescreen),
    # which moves it to SCREENING_FAILED. Re-queueing it on a policy bump instead
    # resurrected every past rejection fleet-wide and cleared the operator's
    # stated reason, so a refused artifact could return under a newer policy that
    # never re-derived the original finding.
    await expire_screening_attempts(session, now=now)
    await fail_orphaned_screening_attempts(
        session,
        screener_hotkey=screener_hotkey,
        now=now,
    )
    claim_budget = await _shared_hotkey_claim_budget(
        session,
        screener_hotkey=screener_hotkey,
        now=now,
    )
    if claim_budget is not None:
        limit = min(limit, claim_budget)
        if limit <= 0:
            return []
    has_running_or_backoff = exists(
        select(ScreeningAttempt.attempt_id).where(
            ScreeningAttempt.agent_id == Agent.agent_id,
            or_(
                ScreeningAttempt.status == "running",
                and_(
                    or_(
                        ScreeningAttempt.status == "expired",
                        and_(
                            ScreeningAttempt.status == "failed",
                            ScreeningAttempt.reason_code.in_(
                                PROVIDER_BACKOFF_REASON_CODES
                            ),
                        ),
                    ),
                    ScreeningAttempt.deadline > now,
                    ~exists(
                        select(ScreeningRetryOverride.override_id).where(
                            ScreeningRetryOverride.attempt_id
                            == ScreeningAttempt.attempt_id
                        )
                    ),
                ),
            ),
        )
    )
    rolling_qualified = exists(
        select(BenchmarkRolloutMember.agent_id)
        .join(
            BenchmarkRollout,
            BenchmarkRollout.rollout_id == BenchmarkRolloutMember.rollout_id,
        )
        .where(
            BenchmarkRolloutMember.agent_id == Agent.agent_id,
            BenchmarkRollout.status.in_(("collecting", "blocked_ineligible")),
        )
    )
    missing_v3_screen = (
        (Agent.screening_policy_version < SCREENING_POLICY_VERSION)
        | Agent.screened_image_sha256.is_(None)
        | Agent.screened_image_size_bytes.is_(None)
        | Agent.screened_image_id.is_(None)
        | Agent.screened_image_ref.is_(None)
        | Agent.screened_image_upload_id.is_(None)
        | Agent.screened_image_verified_at.is_(None)
    )
    missing_dataset, prerequisite_admitted = await prerequisite_screening_predicates(
        session
    )
    pending_deferred_review = exists(
        select(AthReview.review_id).where(
            AthReview.agent_id == Agent.agent_id,
            AthReview.status == "pending",
            AthReview.algorithm_provenance["review_kind"].as_string()
            == "deferred_source_review",
            ~exists(
                select(ScreeningAttempt.attempt_id).where(
                    ScreeningAttempt.agent_id == Agent.agent_id,
                    ScreeningAttempt.build_only.is_(False),
                    ScreeningAttempt.status.in_(("passed", "quarantined")),
                    ScreeningAttempt.started_at
                    >= func.coalesce(AthReview.reopened_at, AthReview.opened_at),
                )
            ),
        )
    )
    # Deliberately NOT gated on ``deferred_review_mode``. An open deferred hold
    # is an obligation the queue already took on, and the only way it clears is
    # a screener re-claiming the agent for its deep pass. Gating this on
    # ``enforce`` (as it once was) meant every flip to another mode froze the
    # holds open at that instant: those agents stay out of the emission-eligible
    # ledger with nothing willing to pick them up again, releasable only one at
    # a time by hand. Draining an already-open queue is bounded work that ends;
    # stranding a miner is not. The mode decides whether NEW holds open, never
    # whether existing ones can be settled.
    deferred_ath_eligible = (
        Agent.status == AgentStatus.ATH_PENDING_REVIEW
    ) & pending_deferred_review
    eligible = or_(
        Agent.status == AgentStatus.UPLOADED,
        Agent.status == AgentStatus.SCREENING_FAILED,
        (
            (Agent.status == AgentStatus.EVALUATING)
            & (Agent.screening_policy_version < SCREENING_POLICY_VERSION)
        ),
        (
            Agent.status.in_((AgentStatus.SCORED, AgentStatus.LIVE))
            & rolling_qualified
            & missing_v3_screen
        ),
        (
            (Agent.status == AgentStatus.EVALUATING)
            & prerequisite_admitted
            & missing_dataset
        ),
        # A submission released from an anti-cheat quarantine back to EVALUATING
        # but without a complete screened image the active version needs is
        # otherwise stuck forever — validators skip it and nothing re-screens it.
        (
            (Agent.status == AgentStatus.EVALUATING)
            & prerequisite_admitted
            & missing_active_screened_image()
        ),
        deferred_ath_eligible,
    )
    candidate_payment = aliased(EvaluationPayment)
    earlier = aliased(Agent)
    earlier_payment = aliased(EvaluationPayment)
    direct_owner_link = exists(
        select(OwnerAttestation.attestation_id).where(
            OwnerAttestation.netuid == netuid,
            OwnerAttestation.revoked_at.is_(None),
            or_(
                and_(
                    OwnerAttestation.hotkey_lo == Agent.miner_hotkey,
                    OwnerAttestation.hotkey_hi == earlier.miner_hotkey,
                ),
                and_(
                    OwnerAttestation.hotkey_lo == earlier.miner_hotkey,
                    OwnerAttestation.hotkey_hi == Agent.miner_hotkey,
                ),
            ),
        )
    )
    earlier_is_different_owner = and_(
        earlier.miner_hotkey != Agent.miner_hotkey,
        ~direct_owner_link,
        or_(
            candidate_payment.miner_coldkey.is_(None),
            earlier_payment.miner_coldkey.is_(None),
            earlier_payment.miner_coldkey != candidate_payment.miner_coldkey,
        ),
    )
    earlier_pending = exists(
        select(earlier.agent_id)
        .select_from(earlier)
        .outerjoin(
            earlier_payment,
            earlier_payment.agent_id == earlier.agent_id,
        )
        .where(
            earlier.sha256 == Agent.sha256,
            earlier_is_different_owner,
            (earlier.created_at < Agent.created_at)
            | (
                (earlier.created_at == Agent.created_at)
                & (earlier.agent_id < Agent.agent_id)
            ),
            earlier.status.in_(
                (
                    AgentStatus.UPLOADED,
                    AgentStatus.SCREENING,
                    AgentStatus.SCREENING_FAILED,
                )
            ),
        )
    )
    agents = list(
        await session.scalars(
            select(Agent)
            .outerjoin(
                candidate_payment,
                candidate_payment.agent_id == Agent.agent_id,
            )
            .where(eligible, ~has_running_or_backoff, ~earlier_pending)
            .order_by(*screening_priority_order())
            .limit(limit)
            .with_for_update(of=Agent, skip_locked=True)
        )
    )
    claimed: list[tuple[Agent, ScreeningAttempt, UUID | None]] = []
    for agent in agents:
        # An agent that keeps coming back inconclusive expires its lease every
        # cycle; after the cap, park it for operator review instead of leasing
        # it out again to loop forever.
        if (
            await _expired_attempt_count(session, agent_id=agent.agent_id)
            >= MAX_SCREENING_EXPIRIES
        ):
            await _park_repeatedly_inconclusive(
                session, agent, screener_hotkey=screener_hotkey, now=now
            )
            continue
        owner = aliased(Agent)
        owner_payment = aliased(EvaluationPayment)
        candidate_coldkey = await session.scalar(
            select(EvaluationPayment.miner_coldkey).where(
                EvaluationPayment.agent_id == agent.agent_id
            )
        )
        owner_has_direct_link = exists(
            select(OwnerAttestation.attestation_id).where(
                OwnerAttestation.netuid == netuid,
                OwnerAttestation.revoked_at.is_(None),
                or_(
                    and_(
                        OwnerAttestation.hotkey_lo == agent.miner_hotkey,
                        OwnerAttestation.hotkey_hi == owner.miner_hotkey,
                    ),
                    and_(
                        OwnerAttestation.hotkey_lo == owner.miner_hotkey,
                        OwnerAttestation.hotkey_hi == agent.miner_hotkey,
                    ),
                ),
            )
        )
        owner_is_different = and_(
            owner.miner_hotkey != agent.miner_hotkey,
            ~owner_has_direct_link,
        )
        if candidate_coldkey is not None:
            owner_is_different = and_(
                owner_is_different,
                or_(
                    owner_payment.miner_coldkey.is_(None),
                    owner_payment.miner_coldkey != candidate_coldkey,
                ),
            )
        # An artifact refused FOR CAUSE stays a valid duplicate owner. Scoping
        # owners to live statuses alone meant banning an original disarmed this
        # check for its clones: the very act of refusing the first copy removed
        # the row that would flag the next one.
        #
        # "For cause" is read from quarantine history rather than the agent row,
        # because a re-screen clears screening_reason_code. An active hold counts
        # (a finding is outstanding); a hold an operator resolved as release or
        # rescreen does not -- they cleared it deliberately.
        #
        # A platform-raised _EXHAUSTED_REASON_CODE park is the exception: it is an
        # infrastructure outcome (the screen never concluded), not a finding about
        # the artifact, so a screener outage must not turn every parked original
        # into grounds for condemning a later identical submission. Once an
        # operator reviews that park and resolves it "reject", the rejection
        # branch below picks it up -- that IS a human judgement for cause.
        refused_for_cause = or_(
            owner.status == AgentStatus.BANNED,
            exists(
                select(ScreeningQuarantine.quarantine_id).where(
                    ScreeningQuarantine.agent_id == owner.agent_id,
                    or_(
                        (ScreeningQuarantine.status == "active")
                        & (ScreeningQuarantine.reason_code != _EXHAUSTED_REASON_CODE),
                        ScreeningQuarantine.resolution == "reject",
                    ),
                )
            ),
        )
        duplicate_of = await session.scalar(
            select(owner.agent_id)
            .outerjoin(
                owner_payment,
                owner_payment.agent_id == owner.agent_id,
            )
            .where(
                owner.sha256 == agent.sha256,
                owner_is_different,
                owner.agent_id != agent.agent_id,
                (owner.created_at < agent.created_at)
                | (
                    (owner.created_at == agent.created_at)
                    & (owner.agent_id < agent.agent_id)
                ),
                or_(
                    owner.status.in_(_USABLE_OWNER_STATUSES),
                    owner.status.in_(_ADJUDICATED_NEGATIVE_OWNER_STATUSES)
                    & refused_for_cause,
                ),
            )
            .order_by(owner.created_at.asc(), owner.agent_id.asc())
            .limit(1)
        )
        has_history = await session.scalar(
            select(exists().where(ScreeningAttempt.agent_id == agent.agent_id))
        )
        if not has_history and agent.screening_policy_version > 0:
            legacy_status = {
                AgentStatus.EVALUATING: "passed",
                AgentStatus.REJECTED: "rejected",
                AgentStatus.SCREENING_FAILED: "failed",
            }.get(agent.status)
            if legacy_status is not None:
                session.add(
                    ScreeningAttempt(
                        attempt_id=uuid4(),
                        agent_id=agent.agent_id,
                        screener_hotkey=screener_hotkey,
                        policy_version=agent.screening_policy_version,
                        status=legacy_status,
                        started_at=agent.created_at,
                        deadline=agent.created_at,
                        finished_at=agent.created_at,
                        public_reason=agent.screening_reason,
                    )
                )
        # An EVALUATING agent on the current policy already cleared the anti-cheat
        # review (it passed, or an operator released its quarantine). Re-claiming
        # it — to rebuild a missing screened image or dataset — must NOT re-run
        # that review, or we would re-judge an approved artifact (and risk a
        # release/re-quarantine loop). It is a build-only pass. A fresh
        # (UPLOADED), failed, or stale-policy submission still gets the full
        # review.
        deferred_deep_review = agent.status == AgentStatus.ATH_PENDING_REVIEW
        # ``enforce`` defers the deep review to the submissions that qualify;
        # ``bypass`` never runs it at all. Both admit on the same cheap
        # build-only pass, so the pre-score depth is one predicate over the two.
        mechanical_first = deferred_review_mode in {
            "enforce",
            "bypass",
        } and agent.status in {
            AgentStatus.UPLOADED,
            AgentStatus.SCREENING_FAILED,
        }
        build_only = not deferred_deep_review and (
            mechanical_first
            or (
                agent.status == AgentStatus.EVALUATING
                and agent.screening_policy_version >= SCREENING_POLICY_VERSION
            )
        )
        attempt = ScreeningAttempt(
            attempt_id=uuid4(),
            agent_id=agent.agent_id,
            screener_hotkey=screener_hotkey,
            policy_version=SCREENING_POLICY_VERSION,
            status="running",
            started_at=now,
            deadline=now + ttl,
            reason_code=(
                "exact-cross-miner-duplicate"
                if duplicate_of is not None
                else _DEFERRED_MECHANICAL_REASON
                if mechanical_first
                else None
            ),
            duplicate_of=duplicate_of,
            build_only=build_only,
        )
        session.add(attempt)
        if agent.status not in (
            AgentStatus.SCORED,
            AgentStatus.LIVE,
            AgentStatus.ATH_PENDING_REVIEW,
        ):
            agent.status = AgentStatus.SCREENING
        agent.screening_reason = None
        agent.screening_reason_code = None
        claimed.append((agent, attempt, duplicate_of))
    await session.flush()
    return claimed


async def get_screening_attempt(
    session: AsyncSession,
    *,
    attempt_id: UUID,
    for_update: bool = False,
) -> ScreeningAttempt | None:
    stmt = select(ScreeningAttempt).where(ScreeningAttempt.attempt_id == attempt_id)
    if for_update:
        stmt = stmt.with_for_update()
    return (await session.scalars(stmt)).one_or_none()


async def list_screening_attempts(
    session: AsyncSession, *, agent_id: UUID
) -> list[ScreeningAttempt]:
    return list(
        await session.scalars(
            select(ScreeningAttempt)
            .where(ScreeningAttempt.agent_id == agent_id)
            .order_by(
                ScreeningAttempt.started_at.desc(),
                ScreeningAttempt.attempt_id.desc(),
            )
        )
    )


async def get_running_screening_attempts(
    session: AsyncSession, *, agent_ids: list[UUID]
) -> dict[UUID, ScreeningAttempt]:
    if not agent_ids:
        return {}
    attempts = await session.scalars(
        select(ScreeningAttempt).where(
            ScreeningAttempt.agent_id.in_(agent_ids),
            ScreeningAttempt.status == "running",
        )
    )
    return {attempt.agent_id: attempt for attempt in attempts}
