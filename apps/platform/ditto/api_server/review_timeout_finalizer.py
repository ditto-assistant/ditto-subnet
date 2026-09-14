"""Policy v13 deadline finalizer: no-fault ``review_timed_out`` for stale reviews.

Policy v13 requires every non-decisive processing state (``inconclusive``,
escalate-without-finding, unavailable court, exhausted expiry cap) to
terminate by a published deadline. Left alone, a Targon runtime lane that can
only ever report ``INCONCLUSIVE`` under v13 would strand every submission it
screens in an operator hold. Converting that timeout into ``REJECT`` would
punish honest miners for a screener outage, so the Platform terminates it as a
distinct decision:

* ``review_timed_out`` -- never a plain ``REJECT``, never a ban;
* ``violation_proven: false``, ``precedent_weight: false`` (a timeout can never
  be cited against the miner, a sibling, or the hotkey);
* a public no-fault reason, the failure domain (V2 platform / V3 provider),
  ``retry_count`` and ``independent_workers`` as retry evidence;
* an automatic no-fault retry grant on the exact attempt so the submission is
  claimable again and, because its last service time is the oldest in the
  queue, re-enters screening ahead of newer work once capacity recovers.

A quarantine that carries a verified finding is an operator hold, not a
processing state, and is never touched. Versions below the strict two-outcome
policy keep their signed compatibility behaviour and are never finalized.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import Text, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.agent_status import AgentStatus
from ditto.db.models import (
    Agent,
    Score,
    ScoredPolicyRescreenRelease,
    ScreeningAttempt,
    ScreeningDecisionRecord,
    ScreeningQuarantine,
    ScreeningQuarantineResolution,
    ScreeningRetryOverride,
)
from ditto.db.queries.benchmark_rollout import arrival_bench_version
from ditto_screening_protocol import (
    NON_DECISIVE_REASON_CODES,
    PUBLISHED_REVIEW_TIMEOUT_POLICY,
    REVIEW_TIMED_OUT_OUTCOME,
    REVIEW_TIMED_OUT_PUBLIC_REASON,
    REVIEW_TIMED_OUT_REASON_CODE,
    REVIEW_TIMEOUT_FINALIZER_ACTOR,
    FailureDomain,
    ReviewTimeoutPolicy,
    ScreeningDecisionOutcome,
    failure_domain_for_reason_code,
    verification_failure_reason_code,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

logger = logging.getLogger(__name__)

DEFAULT_FINALIZER_INTERVAL_SECONDS = 300
DEFAULT_FINALIZER_BATCH = 25


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def record_screening_decision(
    session: AsyncSession,
    *,
    agent: Agent,
    outcome: ScreeningDecisionOutcome,
    reason_codes: list[str],
    violation_proven: bool,
    failure_domain: FailureDomain,
    retry_count: int,
    independent_workers: int,
    policy_version: int,
    public_reason: str,
    reviewer: str,
    decided_at: datetime,
    evidence_references: list[str],
    completed_checks: list[str],
    failed_checks: list[str],
    limitations: list[str],
    attempt_id: UUID | None = None,
    quarantine_id: UUID | None = None,
    review_id: UUID | None = None,
    review_scope: str | None = None,
    evidence_type: str | None = None,
    opaque_components: list[str] | None = None,
    policy_digest: str | None = None,
    verification_profile_digest: str | None = None,
    precedent_weight: bool = False,
    retry_grant_id: UUID | None = None,
    operator_override: dict[str, object] | None = None,
) -> ScreeningDecisionRecord:
    """Append one policy-v13 decision record for ``agent``.

    The previous record for the same agent (if any) is linked through
    ``supersedes_decision`` so appeals and re-reviews keep their history.
    """
    previous_id = await session.scalar(
        select(ScreeningDecisionRecord.decision_id)
        .where(ScreeningDecisionRecord.agent_id == agent.agent_id)
        .order_by(
            ScreeningDecisionRecord.decided_at.desc(),
            ScreeningDecisionRecord.decision_id.desc(),
        )
        .limit(1)
    )
    try:
        benchmark_version: int | None = await arrival_bench_version(
            session, agent=agent
        )
    except Exception:  # pragma: no cover - identity is best-effort evidence
        benchmark_version = None
    record = ScreeningDecisionRecord(
        decision_id=uuid4(),
        agent_id=agent.agent_id,
        attempt_id=attempt_id,
        quarantine_id=quarantine_id,
        review_id=review_id,
        outcome=outcome,
        reason_codes=list(reason_codes),
        violation_proven=violation_proven,
        failure_domain=failure_domain,
        retry_count=retry_count,
        independent_workers=independent_workers,
        policy_version=policy_version,
        identities={
            "submission_uuid": str(agent.agent_id),
            "artifact_sha256": agent.sha256,
            "image_digest": agent.screened_image_id,
            "build_configuration": None,
            "served_entrypoint": None,
            "permitted_runtime_configuration": agent.dataset_run_size,
            "benchmark_version": benchmark_version,
            "applied_policy_version": policy_version,
            "policy_digest": policy_digest,
            "verification_profile_digest": verification_profile_digest,
        },
        review_scope=review_scope,
        completed_checks=list(completed_checks),
        failed_checks=list(failed_checks),
        opaque_components=list(opaque_components or []),
        evidence_references=list(evidence_references),
        evidence_type=evidence_type,
        limitations=list(limitations),
        public_reason=public_reason,
        reviewer=reviewer,
        decided_at=decided_at,
        supersedes_decision=previous_id,
        operator_override=operator_override,
        precedent_weight=precedent_weight,
        retry_grant_id=retry_grant_id,
    )
    session.add(record)
    await session.flush()
    return record


async def _retry_evidence(
    session: AsyncSession, *, agent_id: UUID, from_policy_version: int
) -> tuple[int, int]:
    """Attempts run under the strict policy and the distinct workers that ran them."""
    row = (
        await session.execute(
            select(
                func.count(ScreeningAttempt.attempt_id),
                func.count(func.distinct(ScreeningAttempt.screener_hotkey)),
            ).where(
                ScreeningAttempt.agent_id == agent_id,
                ScreeningAttempt.policy_version >= from_policy_version,
            )
        )
    ).one()
    return int(row[0] or 0), int(row[1] or 0)


async def select_timed_out_quarantines(
    session: AsyncSession,
    *,
    now: datetime,
    policy: ReviewTimeoutPolicy = PUBLISHED_REVIEW_TIMEOUT_POLICY,
    limit: int = DEFAULT_FINALIZER_BATCH,
) -> list[UUID]:
    """Active non-decisive v13+ quarantines older than the published window.

    A quarantine with a finding (or a finding digest bound into the signed
    verdict) is an operator hold with evidence and is excluded: the deadline
    governs processing states, not decisions awaiting a human.
    """
    cutoff = now - policy.max_verification_window
    return list(
        await session.scalars(
            select(ScreeningQuarantine.quarantine_id)
            .where(
                ScreeningQuarantine.status == "active",
                ScreeningQuarantine.reason_code.in_(sorted(NON_DECISIVE_REASON_CODES)),
                # The JSON column persists Python None as JSON null, not SQL
                # NULL; the locked re-check below reads the ORM value.
                or_(
                    ScreeningQuarantine.finding.is_(None),
                    cast(ScreeningQuarantine.finding, Text) == "null",
                ),
                ScreeningQuarantine.finding_digest.is_(None),
                ScreeningQuarantine.policy_version
                >= policy.applies_from_policy_version,
                ScreeningQuarantine.created_at <= cutoff,
            )
            .order_by(ScreeningQuarantine.created_at, ScreeningQuarantine.quarantine_id)
            .limit(limit)
        )
    )


async def finalize_timed_out_quarantine(
    session: AsyncSession,
    *,
    quarantine_id: UUID,
    now: datetime,
    policy: ReviewTimeoutPolicy = PUBLISHED_REVIEW_TIMEOUT_POLICY,
) -> ScreeningDecisionRecord | None:
    """Terminate one stale processing state as no-fault ``review_timed_out``.

    Re-checks every selection predicate under row locks so a concurrent
    operator resolution or a late screener verdict wins. Returns the decision
    record written, or ``None`` when the row no longer qualifies.
    """
    quarantine = await session.scalar(
        select(ScreeningQuarantine)
        .where(ScreeningQuarantine.quarantine_id == quarantine_id)
        .with_for_update()
    )
    if quarantine is None or quarantine.status != "active":
        return None
    if (
        quarantine.reason_code not in NON_DECISIVE_REASON_CODES
        or quarantine.finding is not None
        or quarantine.finding_digest is not None
        or quarantine.policy_version < policy.applies_from_policy_version
        or _utc(quarantine.created_at) > now - policy.max_verification_window
    ):
        return None
    agent = await session.get(Agent, quarantine.agent_id, with_for_update=True)
    if agent is None:
        return None
    attempt = await session.scalar(
        select(ScreeningAttempt).where(
            ScreeningAttempt.attempt_id == quarantine.attempt_id
        )
    )
    release = await session.scalar(
        select(ScoredPolicyRescreenRelease)
        .where(ScoredPolicyRescreenRelease.attempt_id == quarantine.attempt_id)
        .with_for_update()
    )
    # A hold on a fresh submission parks the agent QUARANTINED; a hold raised
    # on a scored policy canary keeps the board row and pauses the release
    # instead. Anything else is a row some other path already moved on.
    if release is None and agent.status != AgentStatus.QUARANTINED:
        return None
    if release is not None and release.state != "paused":
        return None

    retry_count, independent_workers = await _retry_evidence(
        session,
        agent_id=agent.agent_id,
        from_policy_version=policy.applies_from_policy_version,
    )
    failure_domain = failure_domain_for_reason_code(
        quarantine.reason_code,
        failure_provider=attempt.failure_provider if attempt is not None else None,
    )
    v_code = verification_failure_reason_code(failure_domain)
    reason_codes = [REVIEW_TIMED_OUT_REASON_CODE, quarantine.reason_code]
    if v_code is not None:
        reason_codes.insert(1, v_code)

    quarantine.status = "resolved"
    quarantine.resolved_at = now
    quarantine.resolved_by = REVIEW_TIMEOUT_FINALIZER_ACTOR
    # "rescreen" is the physical effect the queue understands; the decision
    # record carries the distinct review_timed_out identity.
    quarantine.resolution = "rescreen"
    quarantine.resolution_reason = REVIEW_TIMED_OUT_PUBLIC_REASON
    session.add(
        ScreeningQuarantineResolution(
            resolution_id=uuid4(),
            quarantine_id=quarantine.quarantine_id,
            resolution="rescreen",
            reason=REVIEW_TIMED_OUT_PUBLIC_REASON,
            actor=REVIEW_TIMEOUT_FINALIZER_ACTOR,
            created_at=now,
        )
    )

    score_count = int(
        await session.scalar(
            select(func.count())
            .select_from(Score)
            .where(Score.agent_id == agent.agent_id)
        )
        or 0
    )
    retry_grant: ScreeningRetryOverride | None = None
    if policy.no_fault_retry_grant_on_timeout:
        # The append-only grant that makes a parked attempt claimable again.
        # One per attempt: an operator who already granted it keeps authorship.
        retry_grant = await session.scalar(
            select(ScreeningRetryOverride).where(
                ScreeningRetryOverride.attempt_id == quarantine.attempt_id
            )
        )
        if retry_grant is None:
            retry_grant = ScreeningRetryOverride(
                override_id=uuid4(),
                agent_id=agent.agent_id,
                attempt_id=quarantine.attempt_id,
                artifact_sha256=agent.sha256,
                expected_score_count=score_count,
                force_full_review=False,
                review_settings_revision=None,
                reason=REVIEW_TIMED_OUT_PUBLIC_REASON,
                actor=REVIEW_TIMEOUT_FINALIZER_ACTOR,
                created_at=now,
            )
            session.add(retry_grant)
            await session.flush()

    if release is None:
        # Parked, claimable through the grant, and served before newer work
        # because its last service time is the oldest in the queue.
        agent.status = AgentStatus.SCREENING_FAILED
    else:
        # The scored canary keeps its board row; re-arming the release is the
        # same automatic retry an operator's retry_paused performs.
        release.state = "pending"
        release.attempt_id = None
        release.actor = REVIEW_TIMEOUT_FINALIZER_ACTOR
        release.reason = REVIEW_TIMED_OUT_PUBLIC_REASON
        release.updated_at = now
    agent.screening_reason = REVIEW_TIMED_OUT_PUBLIC_REASON
    agent.screening_reason_code = REVIEW_TIMED_OUT_REASON_CODE

    completed_checks = ["archive-sha256"]
    if agent.screened_image_id is not None:
        completed_checks.append("image-build")
    record = await record_screening_decision(
        session,
        agent=agent,
        outcome=REVIEW_TIMED_OUT_OUTCOME,
        reason_codes=reason_codes,
        violation_proven=False,
        failure_domain=failure_domain,
        retry_count=retry_count,
        independent_workers=independent_workers,
        policy_version=quarantine.policy_version,
        public_reason=REVIEW_TIMED_OUT_PUBLIC_REASON,
        reviewer=REVIEW_TIMEOUT_FINALIZER_ACTOR,
        decided_at=now,
        evidence_references=[],
        completed_checks=completed_checks,
        failed_checks=[quarantine.reason_code],
        limitations=[
            "bounded review exhausted the published verification window "
            "without a decisive finding; no misconduct is alleged",
            f"retry_count={retry_count} independent_workers={independent_workers} "
            f"window_hours={policy.max_verification_window_hours}",
        ],
        attempt_id=quarantine.attempt_id,
        quarantine_id=quarantine.quarantine_id,
        review_scope="policy-v13 non-decisive processing state",
        evidence_type="processing-state-timeout",
        policy_digest=quarantine.manifest_digest,
        verification_profile_digest=quarantine.review_audit_digest,
        precedent_weight=False,
        retry_grant_id=retry_grant.override_id if retry_grant is not None else None,
    )
    logger.info(
        "review_timed_out agent_id=%s quarantine_id=%s reason_code=%s domain=%s "
        "retry_count=%s independent_workers=%s canary=%s",
        agent.agent_id,
        quarantine.quarantine_id,
        quarantine.reason_code,
        failure_domain,
        retry_count,
        independent_workers,
        release is not None,
    )
    return record


async def finalize_review_timeouts(
    session_maker: async_sessionmaker,
    *,
    now: datetime | None = None,
    policy: ReviewTimeoutPolicy = PUBLISHED_REVIEW_TIMEOUT_POLICY,
    limit: int = DEFAULT_FINALIZER_BATCH,
) -> dict[str, int]:
    """One bounded finalizer pass. Returns counters for tick logs and tests."""
    now = now or datetime.now(UTC)
    async with session_maker() as session:
        candidates = await select_timed_out_quarantines(
            session, now=now, policy=policy, limit=limit
        )
    counters = {"candidates": len(candidates), "finalized": 0, "skipped": 0}
    for quarantine_id in candidates:
        async with session_maker() as session, session.begin():
            record = await finalize_timed_out_quarantine(
                session, quarantine_id=quarantine_id, now=now, policy=policy
            )
        if record is None:
            counters["skipped"] += 1
        else:
            counters["finalized"] += 1
    return counters


class ReviewTimeoutFinalizer:
    """Periodically terminate stale v13 processing states as no-fault timeouts."""

    def __init__(
        self,
        *,
        session_maker: async_sessionmaker,
        interval_seconds: float = DEFAULT_FINALIZER_INTERVAL_SECONDS,
        policy: ReviewTimeoutPolicy = PUBLISHED_REVIEW_TIMEOUT_POLICY,
    ) -> None:
        self._session_maker = session_maker
        self._interval_seconds = interval_seconds
        self._policy = policy
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="review-timeout-finalizer")

    async def aclose(self) -> None:
        self._stop.set()
        task = self._task
        self._task = None
        if task is not None:
            await task

    async def _run(self) -> None:
        while not self._stop.is_set():
            with suppress(Exception):
                await self.tick()
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self._interval_seconds
                )
            except TimeoutError:
                continue

    async def tick(self) -> dict[str, int]:
        counters = await finalize_review_timeouts(
            self._session_maker, policy=self._policy
        )
        if counters["finalized"]:
            logger.info("review-timeout finalizer tick %s", counters)
        return counters
