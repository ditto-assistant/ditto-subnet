"""Leased screening attempts and their append-only public history."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import (
    ColumnElement,
    and_,
    case,
    distinct,
    exists,
    false,
    func,
    lateral,
    or_,
    select,
    true,
)
from sqlalchemy.orm import aliased
from sqlalchemy.sql.selectable import ScalarSelect

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.benchmark_contract import benchmark_contracts
from ditto.api_models.screener_review_settings import (
    INTEGRITY_DOUBLE_CHECK_SCOPE,
)
from ditto.db.models import (
    Agent,
    BenchmarkDataset,
    BenchmarkRollout,
    BenchmarkRolloutMember,
    EvaluationPayment,
    OwnerAttestation,
    Score,
    ScoredPolicyRescreenRelease,
    ScreenerHeartbeat,
    ScreenerReviewSettingsRevision,
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
from ditto.db.queries.screening_infra_retry import (
    InfraRetryDecision,
    InfraSignature,
    plan_infra_retries,
    screener_provider,
)
from ditto.db.queries.screening_retry import (
    failed_screening_retry_authorized,
)
from ditto.db.queries.screening_review_deadlines import record_first_v13_claim_window
from ditto.screener_policy_state import (
    effective_screening_policy_version,
)
from ditto_screening_protocol import reason_codes

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

# Historical cap retained for classifying legacy repeated-inconclusive evidence.
# Current failures park after their first terminal attempt and require an exact
# operator-issued ScreeningRetryOverride before claim selection can run them again.
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
# Mirrors ``ditto.api_server.deferred_source_review.INTEGRITY_DOUBLE_CHECK_TRIGGER``
# without importing the API layer into queries.

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
EXHAUSTED_REASON_CODE = reason_codes.REPEATEDLY_INCONCLUSIVE
"""Public alias: the emission-eligibility gate
(:mod:`ditto.api_server.emission_eligibility`) classifies this code as
inconclusive, and must not carry its own copy of the string."""
_EXHAUSTED_REASON_CODE = EXHAUSTED_REASON_CODE
_EXHAUSTED_PUBLIC_REASON = "Screening expired repeatedly; manual retry required"
POLICY_ONLY_RESCREEN_REASON = "policy-only-rescreen"
_ORPHANED_ATTEMPT_REASON_CODE = reason_codes.WORKER_LEASE_ORPHANED
_ORPHANED_ATTEMPT_REASON = (
    "Screening worker stopped reporting this attempt; manual retry required"
)
# Unlike an orphan, an expiry still counts toward ``MAX_SCREENING_EXPIRIES`` and
# backs off by status, so this code joins no retry or backoff set.
LEASE_EXPIRED_REASON_CODE = reason_codes.SCREENING_LEASE_EXPIRED
_LEASE_EXPIRED_REASON = "Screening lease expired"
# Provider/reviewer failures held on reclaim for ``FAILED_ATTEMPT_RETRY_BACKOFF`` and,
# with peer-pass evidence, counted toward ``MAX_SCREENING_EXPIRIES``
# (``_inconclusive_attempt_count``). Do not add a code that should retry
# automatically WITHOUT counting: those live in
# ``screening_infra_retry.INFRA_AUTO_RETRY_REASON_CODES``, which has its own
# backoff/breaker and never feeds the park cap. The codes are the
# ``PROVIDER_BACKOFF`` class of the shared screening registry
# (``ditto_screening_protocol.reason_codes``), which records why each is here.
PROVIDER_BACKOFF_REASON_CODES: tuple[str, ...] = (
    reason_codes.PROVIDER_BACKOFF_REASON_CODES
)
# How long a provider-backoff failure waits after its FAILURE before the agent
# is claimable again, capped by the attempt deadline. Backing off to the full
# 70-minute lease deadline stranded a review that died 20 minutes in for the
# remaining 50 idle minutes (2026-08-28 incident, raised by miners as a stuck
# queue); ten minutes still prevents hot-looping a broken provider while the
# peer-evidence rule keeps outage failures from burning the attempt cap.
FAILED_ATTEMPT_RETRY_BACKOFF = timedelta(minutes=10)
# Active workers report at least every two minutes. Wait through two complete
# heartbeat intervals before inferring an orphan, and only act on heartbeat
# observations fresh enough to classify that worker as online publicly.
_ORPHANED_ATTEMPT_GRACE = timedelta(minutes=5)
_SCREENER_HEARTBEAT_FRESHNESS = timedelta(minutes=5)
_SCREENING_CLAIM_LOCK_KEY = 0x445554544F534352


async def try_acquire_screening_claim_lock(session: AsyncSession) -> bool:
    """Acquire the claim serializer without queueing behind another poller.

    Exact-hash ownership and shared-hotkey capacity are decided across several
    rows, so claims still need one transaction-wide critical section. Polling
    workers do not need to wait for it: a busy gate means another claim is
    already making progress, and returning an empty claim avoids a lock convoy.
    """
    if session.get_bind().dialect.name != "postgresql":
        return True
    return bool(
        await session.scalar(
            select(func.pg_try_advisory_xact_lock(_SCREENING_CLAIM_LOCK_KEY))
        )
    )


def screening_running_or_backoff(now: datetime) -> ColumnElement[bool]:
    """Match an agent that holds a running lease or is still backing off.

    A failed attempt backs off from its FAILURE time, not to the end of the
    70-minute lease it no longer occupies: holding a review that died 20
    minutes in until minute 70 stranded agents for most of an hour and read
    as a stuck queue from the public pipeline. The short hold still prevents
    hot-looping against a broken provider, the lease deadline stays the
    upper bound, and an attempt with no recorded finish keeps the historical
    deadline behavior. An operator retry for the attempt waives its hold.
    """
    backoff_until = func.least(
        ScreeningAttempt.deadline,
        func.coalesce(
            ScreeningAttempt.finished_at + FAILED_ATTEMPT_RETRY_BACKOFF,
            ScreeningAttempt.deadline,
        ),
    )
    return exists(
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
                    backoff_until > now,
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


def screening_deferred_behind_earlier_owner(
    candidate_payment: type[EvaluationPayment], *, netuid: int
) -> ColumnElement[bool]:
    """Match an agent whose bytes an earlier, different owner still holds.

    While the earlier submission is uploaded, screening, or failed awaiting a
    retry, the claim defers this one so the duplicate race resolves before
    either is judged. ``candidate_payment`` must be outer-joined to the
    agent's own payment row by the caller.
    """
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
    return exists(
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


async def has_claimable_screening_work(
    session: AsyncSession,
    *,
    now: datetime,
    netuid: int,
) -> bool:
    """Report whether a fresh submission is waiting for this claimant.

    Covers the two fresh-work arms of ``claim_screening_attempts``: a new
    upload, and a failed screen whose exact latest attempt carries a Backroom
    retry authorization. It applies the claim's own exclusions from the same
    predicate builders, so work production would never select cannot hold
    another lane back indefinitely:

    - a running lease or backoff hold (``screening_running_or_backoff``);
    - a duplicate deferred behind an earlier owner's in-flight submission
      (``screening_deferred_behind_earlier_owner``), which can stay parked
      until an operator acts on the earlier one.

    Retry review postures are no longer honoured by the claim (source review
    retired 2026-11-07), so they do not narrow this read either.

    The other claim arms are not modeled: the released scored-policy rescreen
    (SCORED/LIVE), infrastructure auto-retries, stale EVALUATING rows and
    deferred ATH passes. They do not hold a yielding lane, whose own
    reservation of admitted workers covers them.

    This is an advisory read for lanes that must yield to production, such as
    report-only canaries. It is one unlocked ``SELECT``: it takes no advisory
    lock, locks no rows, runs no expiry or orphan sweep, and writes nothing, so
    it never queues behind or delays a production claim.
    """
    candidate_payment = aliased(EvaluationPayment)
    claimable = and_(
        ~screening_running_or_backoff(now),
        ~screening_deferred_behind_earlier_owner(candidate_payment, netuid=netuid),
    )

    def fresh_work(*arm: ColumnElement[bool]) -> ColumnElement[bool]:
        return exists(
            select(Agent.agent_id)
            .outerjoin(candidate_payment, candidate_payment.agent_id == Agent.agent_id)
            .where(*arm, claimable)
        )

    # Two EXISTS arms rather than one OR so the upload arm stays on the
    # partial ``agents_status_uploaded_idx`` index.
    uploaded = fresh_work(Agent.status == AgentStatus.UPLOADED)
    authorized_retry = fresh_work(
        Agent.status == AgentStatus.SCREENING_FAILED,
        failed_screening_retry_authorized(),
    )
    return bool(await session.scalar(select(or_(uploaded, authorized_retry))))


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
            ScreeningAttempt.policy_version == effective_screening_policy_version(),
        )
        .correlate(Agent)
        .scalar_subquery()
    )
    return func.coalesce(latest_attempt, Agent.created_at)


def screening_score_aggregates() -> tuple[Any, Any]:
    """One LATERAL aggregate pair per candidate agent, outerjoined by callers.

    The three aggregates the screening order needs (accepted-score count,
    average composite, and the latest current-policy screening-consumption
    timestamp) were previously three separate correlated scalar subqueries
    inside the ORDER BY, so PostgreSQL re-evaluated each of them for every
    candidate row on every sort term — the production audit measured an empty
    claim spending 6.5-13.7 s inside the claim transaction. Grouping them per
    agent once and joining the result fixed that, but a plain grouped relation
    has no WHERE on the candidate set, so every claim aggregated the entire
    ``scores`` table. Each aggregate is now LATERAL: correlated on the outer
    agent, so the planner probes one agent's rows per candidate (an index
    lookup against the claimable set) instead of one whole-table aggregation
    pass per claim, while each aggregate still runs exactly once per row.
    """
    score_count = lateral(
        select(
            func.count(Score.composite).label("score_count"),
            func.avg(Score.composite).label("provisional_composite"),
        )
        .where(Score.agent_id == Agent.agent_id)
        .correlate(Agent)
        .subquery()
    )
    last_served = lateral(
        select(
            func.max(
                func.coalesce(
                    ScreeningAttempt.finished_at,
                    ScreeningAttempt.deadline,
                    ScreeningAttempt.started_at,
                )
            ).label("last_served_at"),
        )
        .where(
            ScreeningAttempt.agent_id == Agent.agent_id,
            ScreeningAttempt.policy_version == effective_screening_policy_version(),
        )
        .correlate(Agent)
        .subquery()
    )
    return (score_count, last_served)


def screening_priority_order(
    aggregates: tuple[Any, Any] | None = None,
) -> tuple[ColumnElement[Any], ...]:
    """Prioritize finalists while interleaving bounded screening retries.

    A policy bump can return stale non-scored work, while an existing board
    score needs an explicit rollout release. Submissions already one score from
    quorum should not lose their chance to finalize behind that limited queue.
    Within each lane, the least recently served
    submission goes first: an expired lease moves an item behind the untouched
    backlog, but it remains ahead of submissions arriving later. This prevents
    either retries or fresh arrivals from monopolizing the worker while
    preserving the existing score and age tie-breakers.

    ``aggregates`` is the optional pair of precomputed grouped relations from
    :func:`screening_score_aggregates`; pass it (and outerjoin it onto the
    caller's select) so the per-agent aggregates are computed once per claim
    rather than re-evaluated per row per ORDER BY term. Omitting it falls back
    to the correlated scalar subqueries, which stays correct but slow — only
    for callers that cannot restructure their select.
    """
    score_count: ColumnElement[Any]
    last_served_at: ColumnElement[Any]
    provisional_composite: ColumnElement[Any]
    if aggregates is None:
        score_count = screening_score_count()
        last_served_at = screening_last_served_at()
        provisional_composite = (
            select(func.avg(Score.composite))
            .where(Score.agent_id == Agent.agent_id)
            .correlate(Agent)
            .scalar_subquery()
        )
    else:
        score_counts, last_served = aggregates
        score_count = func.coalesce(score_counts.c.score_count, 0)
        last_served_at = func.coalesce(last_served.c.last_served_at, Agent.created_at)
        provisional_composite = score_counts.c.provisional_composite
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
            # A verdict already committing this attempt owns the outcome. An
            # expiry sweep must skip it instead of waiting and delaying that
            # verdict; the next sweep can reconsider any row left running.
            .with_for_update(skip_locked=True)
        )
    )
    for attempt in attempts:
        attempt.status = "expired"
        attempt.finished_at = now
        attempt.public_reason = _LEASE_EXPIRED_REASON
        if attempt.reason_code is None:
            # A claim-time code (duplicate precheck, deferred mechanical
            # admission, policy-only rescreen) is the lease's execution mode,
            # which a late verdict is still checked against; keep it.
            attempt.reason_code = LEASE_EXPIRED_REASON_CODE
        release = await session.scalar(
            select(ScoredPolicyRescreenRelease)
            .where(ScoredPolicyRescreenRelease.attempt_id == attempt.attempt_id)
            .with_for_update()
        )
        if release is not None:
            # No worker verdict is a non-verdict. Stop the one-row policy
            # rollout until an operator explicitly retries this canary.
            release.state = "paused"
        agent = await session.get(Agent, attempt.agent_id)
        if agent is not None and agent.status == AgentStatus.SCREENING:
            agent.status = AgentStatus.SCREENING_FAILED
            agent.screening_reason = _LEASE_EXPIRED_REASON
            agent.screening_reason_code = LEASE_EXPIRED_REASON_CODE
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
    ``failed`` so they do not consume the five-expiry adjudication budget. They
    park for an operator retry and are never retried automatically: a worker
    also stops reporting an attempt after Platform rejected its verdict or the
    worker died mid-screen, both of which the artifact can provoke. Overdue
    leases are included when positive orphan evidence exists; otherwise they
    are left for ``expire_screening_attempts``.
    """
    candidates = list(
        (
            await session.execute(
                select(
                    ScreeningAttempt.attempt_id,
                    ScreeningAttempt.agent_id,
                    ScreeningAttempt.started_at,
                ).where(
                    ScreeningAttempt.screener_hotkey == screener_hotkey,
                    ScreeningAttempt.status == "running",
                    ScreeningAttempt.started_at <= now - _ORPHANED_ATTEMPT_GRACE,
                )
            )
        )
        .tuples()
        .all()
    )
    if not candidates:
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
                    [attempt_id for attempt_id, _, _ in candidates]
                ),
                SubmissionImageBuild.status.in_(
                    ("queued", "leased", "running", "succeeded")
                ),
            )
        )
    )

    failed = 0
    for attempt_id, agent_id, candidate_started_at in candidates:
        started_at = candidate_started_at
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
            heartbeat.state == "screening" and heartbeat.active_agent_id == agent_id
            for heartbeat in heartbeats
        )
        if not observed_after_claim or still_active or attempt_id in in_flight_builds:
            continue

        # Only a row that currently looks orphaned is locked. Re-read all
        # positive liveness evidence after obtaining that row lock so a stale
        # candidate snapshot cannot fail an attempt that resumed meanwhile.
        attempt = await session.scalar(
            select(ScreeningAttempt)
            .where(
                ScreeningAttempt.attempt_id == attempt_id,
                ScreeningAttempt.screener_hotkey == screener_hotkey,
                ScreeningAttempt.status == "running",
                ScreeningAttempt.started_at <= now - _ORPHANED_ATTEMPT_GRACE,
            )
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        if attempt is None:
            continue
        fresh_heartbeats = list(
            await session.scalars(
                select(ScreenerHeartbeat)
                .where(
                    ScreenerHeartbeat.screener_hotkey == screener_hotkey,
                    ScreenerHeartbeat.seen_at >= now - _SCREENER_HEARTBEAT_FRESHNESS,
                )
                .execution_options(populate_existing=True)
            )
        )
        attempt_started_at = attempt.started_at
        if attempt_started_at.tzinfo is None:
            attempt_started_at = attempt_started_at.replace(tzinfo=UTC)
        observed_after_lock = any(
            (
                heartbeat.seen_at
                if heartbeat.seen_at.tzinfo is not None
                else heartbeat.seen_at.replace(tzinfo=UTC)
            )
            > attempt_started_at
            for heartbeat in fresh_heartbeats
        )
        active_after_lock = any(
            heartbeat.state == "screening"
            and heartbeat.active_agent_id == attempt.agent_id
            for heartbeat in fresh_heartbeats
        )
        build_after_lock = bool(
            await session.scalar(
                select(
                    exists().where(
                        SubmissionImageBuild.attempt_id == attempt.attempt_id,
                        SubmissionImageBuild.status.in_(
                            ("queued", "leased", "running", "succeeded")
                        ),
                    )
                )
            )
        )
        if not observed_after_lock or active_after_lock or build_after_lock:
            continue
        attempt.status = "failed"
        attempt.finished_at = now
        attempt.public_reason = _ORPHANED_ATTEMPT_REASON
        attempt.reason_code = _ORPHANED_ATTEMPT_REASON_CODE
        release = await session.scalar(
            select(ScoredPolicyRescreenRelease)
            .where(ScoredPolicyRescreenRelease.attempt_id == attempt.attempt_id)
            .with_for_update()
        )
        if release is not None:
            release.state = "paused"
        agent = await session.get(Agent, attempt.agent_id)
        if agent is not None and agent.status == AgentStatus.SCREENING:
            agent.status = AgentStatus.SCREENING_FAILED
            agent.screening_reason = _ORPHANED_ATTEMPT_REASON
            agent.screening_reason_code = _ORPHANED_ATTEMPT_REASON_CODE
        failed += 1
    return failed


async def sweep_screening_leases(
    session: AsyncSession, *, now: datetime, screener_hotkey: str | None = None
) -> tuple[int, int]:
    """Retire orphaned, then overdue, running leases; return both counts.

    The orphan sweep runs first so an overdue attempt with positive orphan
    evidence records an infrastructure failure instead of an expiry.
    ``screener_hotkey`` limits the orphan sweep to that fleet; ``None``
    sweeps every hotkey holding a running attempt. Both sweeps skip or re-read
    locked rows, so callers need not hold the screening claim lock.
    """
    if screener_hotkey is None:
        hotkeys = list(
            await session.scalars(
                select(distinct(ScreeningAttempt.screener_hotkey)).where(
                    ScreeningAttempt.status == "running"
                )
            )
        )
    else:
        hotkeys = [screener_hotkey]
    orphaned = 0
    for hotkey in hotkeys:
        orphaned += await fail_orphaned_screening_attempts(
            session, screener_hotkey=hotkey, now=now
        )
    expired = await expire_screening_attempts(session, now=now)
    return orphaned, expired


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


async def infra_retry_agent_admitted(session: AsyncSession, agent_id: UUID) -> bool:
    """Whether the claim's ``prerequisite_admitted`` guard admits one agent.

    The exact predicate ``claim_screening_attempts`` applies to the automatic
    infrastructure retry, so public text can say "retried automatically" only for
    an agent the claim will actually pick up.
    """
    _, admitted = await prerequisite_screening_predicates(session)
    return (
        await session.scalar(
            select(Agent.agent_id).where(Agent.agent_id == agent_id, admitted)
        )
        is not None
    )


async def _inconclusive_attempt_count(session: AsyncSession, *, agent_id: UUID) -> int:
    """Count inconclusive screening turns under the current policy **since the
    agent's most recent operator clear**.

    Two shapes consume a turn without concluding. An ``expired`` lease always
    counts. A ``failed`` provider-backoff attempt (its reason code is in
    ``PROVIDER_BACKOFF_REASON_CODES``) counts only with peer evidence: some
    OTHER agent's attempt passed inside this attempt's lease window
    (``started_at``..``deadline``), so the screening lane demonstrably worked
    for a peer while repeatedly refusing this artifact. During a genuine
    Targon/Cloud Run outage nothing passes, no failure gains peer evidence,
    and a fleet-wide blip still cannot mass-park every in-flight agent.

    Without the failed-attempt branch, an archive whose image builds but whose
    runtime never comes up (the smoke replica dies on both providers, reported
    as e.g. ``CLOUDRUN_PROVISION_ERROR``) failed ``targon-runtime-unavailable``
    every 70-minute lease forever: the cap counted only expiries, so the agent
    rebuilt Kaniko each cycle and the miner saw "waiting for admission"
    indefinitely instead of an operator-visible hold.

    Resolving a quarantine or authorizing an exact failed-attempt retry
    explicitly lets the submission move forward and therefore grants a fresh
    attempt budget.
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
    last_manual_retry = (
        select(func.max(ScreeningRetryOverride.created_at))
        .where(ScreeningRetryOverride.agent_id == agent_id)
        .scalar_subquery()
    )
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    peer = aliased(ScreeningAttempt)
    peer_passed_in_window = exists(
        select(peer.attempt_id).where(
            peer.agent_id != agent_id,
            peer.policy_version == effective_screening_policy_version(),
            peer.status == "passed",
            peer.finished_at >= ScreeningAttempt.started_at,
            peer.finished_at <= ScreeningAttempt.deadline,
        )
    )
    count = await session.scalar(
        select(func.count())
        .select_from(ScreeningAttempt)
        .where(
            ScreeningAttempt.agent_id == agent_id,
            ScreeningAttempt.policy_version == effective_screening_policy_version(),
            or_(
                ScreeningAttempt.status == "expired",
                and_(
                    ScreeningAttempt.status == "failed",
                    ScreeningAttempt.reason_code.in_(PROVIDER_BACKOFF_REASON_CODES),
                    peer_passed_in_window,
                ),
            ),
            ScreeningAttempt.started_at > func.coalesce(last_operator_clear, epoch),
            ScreeningAttempt.started_at > func.coalesce(last_manual_retry, epoch),
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
    """Park an agent that keeps expiring its lease until an operator retries it.

    Records a terminal ``failed`` attempt and moves the agent to
    ``SCREENING_FAILED``, so it leaves the retry pool instead of burning a
    lease every cycle. It is an infrastructure park, not a hold: no quarantine
    row is opened (source review and its holds are retired, 2026-11-07) and an
    exact Backroom retry authorization makes it claimable again.
    """
    # This is an operator park, not an artifact-bound execution lease.
    session.add(
        ScreeningAttempt(
            attempt_id=uuid4(),
            agent_id=agent.agent_id,
            screener_hotkey=screener_hotkey,
            policy_version=effective_screening_policy_version(),
            status="failed",
            started_at=now,
            deadline=now,
            finished_at=now,
            public_reason=_EXHAUSTED_PUBLIC_REASON,
            reason_code=_EXHAUSTED_REASON_CODE,
        )
    )
    agent.status = AgentStatus.SCREENING_FAILED
    agent.screening_reason = _EXHAUSTED_PUBLIC_REASON
    agent.screening_reason_code = _EXHAUSTED_REASON_CODE


async def latest_integrity_double_check_posture(
    session: AsyncSession,
) -> ScreenerReviewSettingsRevision | None:
    """Return the reviewer revision a double-check deep pass is pinned to."""
    return await session.scalar(
        select(ScreenerReviewSettingsRevision)
        .where(ScreenerReviewSettingsRevision.scope == INTEGRITY_DOUBLE_CHECK_SCOPE)
        .order_by(ScreenerReviewSettingsRevision.revision.desc())
        .limit(1)
    )


async def claim_screening_attempts(
    session: AsyncSession,
    *,
    screener_hotkey: str,
    now: datetime,
    ttl: timedelta,
    limit: int,
    netuid: int = 118,
    review_settings_binding: tuple[int, str, str, str] | None = None,
    claim_lock_held: bool = False,
) -> list[tuple[Agent, ScreeningAttempt, UUID | None]]:
    """Claim completion-lane contenders, then least-scored eligible work.

    Every attempt is the mechanical lane (``build_only``): archive and
    container-contract validation, the static malicious-source preflight, the
    isolated image build, the runtime health contract, and the screened-image
    export. There is no model source review, no policy-only rescreen, no
    scored-policy rescreen and no deferred deep review (owner decision,
    2026-11-07), so no attempt carries the old ``deferred-mechanical-admission``
    marker. A fresh upload or an authorized retry passes straight to validator
    scoring once its screened image is verified; an EVALUATING or
    rollout-member row is re-claimed only to rebuild a missing dataset or
    screened image.

    ``review_settings_binding`` is recorded on the attempt as the claimant's
    applied worker settings. It selects no review posture.

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
    if bind.dialect.name == "postgresql" and not claim_lock_held:
        await session.execute(
            select(func.pg_advisory_xact_lock(_SCREENING_CLAIM_LOCK_KEY))
        )
    # A REJECTED agent is deliberately absent above. It re-enters screening only
    # through the operator appeal (POST /screening-submissions/{id}/rescreen),
    # which moves it to SCREENING_FAILED. Re-queueing it on a policy bump instead
    # resurrected every past rejection fleet-wide and cleared the operator's
    # stated reason, so a refused artifact could return under a newer policy that
    # never re-derived the original finding.
    await sweep_screening_leases(session, now=now, screener_hotkey=screener_hotkey)
    claim_budget = await _shared_hotkey_claim_budget(
        session,
        screener_hotkey=screener_hotkey,
        now=now,
    )
    if claim_budget is not None:
        limit = min(limit, claim_budget)
        if limit <= 0:
            return []
    has_running_or_backoff = screening_running_or_backoff(now)
    # Infrastructure-parked agents retry on their own schedule: per-artifact
    # exponential backoff, then the fleet breaker. Planned under the claim lock
    # from persisted attempts, so every worker derives the same answer.
    infra_plan = await plan_infra_retries(
        session,
        now=now,
        claimant_provider=await screener_provider(session, screener_hotkey),
    )
    infra_decisions = infra_plan.decisions
    infra_auto_retry = (
        Agent.agent_id.in_(infra_plan.claimable_agent_ids)
        if infra_plan.claimable_agent_ids
        else false()
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
        Agent.screened_image_sha256.is_(None)
        | Agent.screened_image_size_bytes.is_(None)
        | Agent.screened_image_id.is_(None)
        | Agent.screened_image_ref.is_(None)
        | Agent.screened_image_upload_id.is_(None)
        | Agent.screened_image_verified_at.is_(None)
    )
    missing_dataset, prerequisite_admitted = await prerequisite_screening_predicates(
        session
    )
    # Per-agent score aggregates are computed once per claim rather
    # than re-evaluated per candidate row per ORDER BY term.
    score_aggregates = screening_score_aggregates()
    manual_failed_retry = failed_screening_retry_authorized()
    eligible = or_(
        Agent.status == AgentStatus.UPLOADED,
        # A failed attempt is parked forever. Only the append-only Backroom
        # authorization for that exact latest attempt makes it claimable.
        (Agent.status == AgentStatus.SCREENING_FAILED) & manual_failed_retry,
        # A submission withdrawn from the active benchmark era must not spend
        # screener capacity on an automatic infrastructure retry.
        infra_auto_retry & prerequisite_admitted,
        # A rollout member that is scored but lacks a verified screened image
        # is rebuilt so validators can lease it on the new contract.
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
        # An EVALUATING submission without a complete screened image the active
        # version needs is otherwise stuck forever: validators skip it and
        # nothing re-screens it.
        (
            (Agent.status == AgentStatus.EVALUATING)
            & prerequisite_admitted
            & missing_active_screened_image()
        ),
    )
    candidate_payment = aliased(EvaluationPayment)
    earlier_pending = screening_deferred_behind_earlier_owner(
        candidate_payment, netuid=netuid
    )
    agents = list(
        await session.scalars(
            select(Agent)
            # LATERAL aggregates carry their own correlation; no ON clause.
            .outerjoin(score_aggregates[0], onclause=true())
            .outerjoin(score_aggregates[1], onclause=true())
            .outerjoin(
                candidate_payment,
                candidate_payment.agent_id == Agent.agent_id,
            )
            .where(
                eligible,
                ~has_running_or_backoff,
                ~earlier_pending,
            )
            .order_by(*screening_priority_order(score_aggregates))
            # Surplus probe candidates only exist to be skipped below; widen the
            # window so they cannot crowd out claimable work behind them.
            .limit(limit + infra_plan.surplus_probe_candidates)
            .with_for_update(of=Agent, skip_locked=True)
        )
    )
    claimed: list[tuple[Agent, ScreeningAttempt, UUID | None]] = []
    probed_signatures: set[InfraSignature] = set()
    for agent in agents:
        if len(claimed) >= limit:
            break
        infra: InfraRetryDecision | None = infra_decisions.get(agent.agent_id)
        # One probe per signature per interval. The probe attempt is added to
        # this locked transaction, so the next claimer sees it.
        if (
            infra is not None
            and infra.state == "probe_due"
            and infra.signature in probed_signatures
        ):
            continue
        # An agent that keeps coming back inconclusive burns a lease every
        # cycle; after the cap, park it for operator review instead of leasing
        # it out again to loop forever.
        if (
            await _inconclusive_attempt_count(session, agent_id=agent.agent_id)
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
        # A policy v13 V2 reject is likewise no finding: verification did not
        # complete, so it records violation_proven false.
        refused_for_cause = or_(
            owner.status == AgentStatus.BANNED,
            exists(
                select(ScreeningQuarantine.quarantine_id).where(
                    ScreeningQuarantine.agent_id == owner.agent_id,
                    or_(
                        (ScreeningQuarantine.status == "active")
                        & (ScreeningQuarantine.reason_code != _EXHAUSTED_REASON_CODE),
                        (ScreeningQuarantine.resolution == "reject")
                        & (
                            ScreeningQuarantine.reason_code
                            != reason_codes.VERIFICATION_INCOMPLETE_UNREVIEWABLE
                        ),
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
                # Historical state has no observed attempt/artifact receipt.
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
        attempt_policy_version = effective_screening_policy_version()
        attempt = ScreeningAttempt(
            attempt_id=uuid4(),
            agent_id=agent.agent_id,
            artifact_sha256=agent.sha256,
            screener_hotkey=screener_hotkey,
            policy_version=attempt_policy_version,
            status="running",
            started_at=now,
            deadline=now + ttl,
            # No deferred-review marker: nothing is deferred. A build-only
            # attempt cannot quarantine, so a fresh upload either passes to
            # EVALUATING or fails a build or safety check.
            reason_code=(
                "exact-cross-miner-duplicate" if duplicate_of is not None else None
            ),
            duplicate_of=duplicate_of,
            build_only=True,
            review_settings_revision=(
                review_settings_binding[0]
                if review_settings_binding is not None
                else None
            ),
            review_settings_instance_id=(
                review_settings_binding[1]
                if review_settings_binding is not None
                else None
            ),
            review_settings_scope=(
                review_settings_binding[2]
                if review_settings_binding is not None
                else None
            ),
            review_settings_checksum=(
                review_settings_binding[3]
                if review_settings_binding is not None
                else None
            ),
        )
        session.add(attempt)
        await record_first_v13_claim_window(
            session, agent=agent, attempt=attempt, lease_ttl=ttl
        )
        if agent.status not in (
            AgentStatus.SCORED,
            AgentStatus.LIVE,
            AgentStatus.ATH_PENDING_REVIEW,
        ):
            agent.status = AgentStatus.SCREENING
        agent.screening_reason = None
        agent.screening_reason_code = None
        claimed.append((agent, attempt, duplicate_of))
        if infra is not None and infra.state == "probe_due":
            probed_signatures.add(infra.signature)
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
