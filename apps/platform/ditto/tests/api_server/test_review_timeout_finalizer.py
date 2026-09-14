"""Policy v13 deadline finalizer: stale non-decisive holds become no-fault timeouts.

The finalizer is the named activation blocker for the strict two-outcome
policy. These tests pin its contract: it converts only strict-policy (v13+)
non-decisive processing states older than the published window, never a hold
with a finding, never an older-policy hold, never a fresh one; the conversion
is a distinct ``review_timed_out`` decision (no ban, no precedent weight,
public no-fault reason, failure domain and retry evidence) with a no-fault
retry grant that makes the submission claimable again ahead of newer work.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.api_server.review_timeout_finalizer import (
    ReviewTimeoutFinalizer,
    finalize_review_timeouts,
    select_timed_out_quarantines,
)
from ditto.db.models import (
    Agent,
    ScoredPolicyRescreenRelease,
    ScreenerPolicyActivation,
    ScreeningAttempt,
    ScreeningDecisionRecord,
    ScreeningQuarantine,
    ScreeningQuarantineResolution,
    ScreeningRetryOverride,
)
from ditto.db.queries.screening import claim_screening_attempts
from ditto.screener_policy_state import update_effective_screener_policy
from ditto_screening_protocol import (
    NON_DECISIVE_REASON_CODES,
    PUBLISHED_REVIEW_TIMEOUT_POLICY,
    REVIEW_TIMED_OUT_OUTCOME,
    REVIEW_TIMED_OUT_PUBLIC_REASON,
    REVIEW_TIMED_OUT_REASON_CODE,
    REVIEW_TIMEOUT_FINALIZER_ACTOR,
    SCREENING_FLOOR_POLICY_VERSION,
    STRICT_TWO_OUTCOME_POLICY_VERSION,
    V2_PLATFORM_VERIFICATION_FAILED,
    V3_PROVIDER_VERIFICATION_FAILED,
)

pytestmark = pytest.mark.asyncio

_SCREENER = "5ScreenerHotkeyAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
_WINDOW = PUBLISHED_REVIEW_TIMEOUT_POLICY.max_verification_window


async def _seed_hold(
    maker: async_sessionmaker[AsyncSession],
    *,
    reason_code: str = "source-review-inconclusive",
    policy_version: int = STRICT_TWO_OUTCOME_POLICY_VERSION,
    age: timedelta = _WINDOW + timedelta(hours=1),
    finding: dict | None = None,
    finding_digest: str | None = None,
    failure_provider: str | None = None,
    extra_attempts: int = 0,
    extra_workers: tuple[str, ...] = (),
    agent_status: AgentStatus = AgentStatus.QUARANTINED,
) -> tuple[UUID, UUID, UUID]:
    """One agent parked in a hold ``age`` ago; returns (agent, attempt, quarantine)."""
    agent_id, attempt_id, quarantine_id = uuid4(), uuid4(), uuid4()
    created = datetime.now(UTC) - age
    async with maker() as session, session.begin():
        session.add(
            Agent(
                agent_id=agent_id,
                miner_hotkey=f"5Miner{agent_id.hex[:8]}",
                name="held",
                sha256=agent_id.hex * 2,
                status=agent_status,
                screening_policy_version=policy_version,
                screening_reason="Bounded source review was inconclusive; held",
                screening_reason_code=reason_code,
                created_at=created - timedelta(hours=1),
            )
        )
        await session.flush()
        for index in range(extra_attempts):
            worker = extra_workers[index] if index < len(extra_workers) else _SCREENER
            session.add(
                ScreeningAttempt(
                    attempt_id=uuid4(),
                    agent_id=agent_id,
                    screener_hotkey=worker,
                    policy_version=policy_version,
                    status="expired",
                    started_at=created - timedelta(hours=2 + index),
                    deadline=created - timedelta(hours=1 + index),
                    finished_at=created - timedelta(hours=1 + index),
                )
            )
        session.add(
            ScreeningAttempt(
                attempt_id=attempt_id,
                agent_id=agent_id,
                screener_hotkey=_SCREENER,
                policy_version=policy_version,
                status="quarantined",
                started_at=created - timedelta(minutes=30),
                deadline=created + timedelta(minutes=30),
                finished_at=created,
                public_reason="held",
                reason_code=reason_code,
                failure_provider=failure_provider,
            )
        )
        await session.flush()
        session.add(
            ScreeningQuarantine(
                quarantine_id=quarantine_id,
                agent_id=agent_id,
                attempt_id=attempt_id,
                screener_hotkey=_SCREENER,
                policy_version=policy_version,
                manifest_digest="a" * 64,
                finding_digest=finding_digest,
                reason_code=reason_code,
                evidence=None,
                finding=finding,
                status="active",
                created_at=created,
            )
        )
    return agent_id, attempt_id, quarantine_id


async def _decisions(
    maker: async_sessionmaker[AsyncSession], agent_id: UUID
) -> list[ScreeningDecisionRecord]:
    async with maker() as session:
        return list(
            await session.scalars(
                select(ScreeningDecisionRecord).where(
                    ScreeningDecisionRecord.agent_id == agent_id
                )
            )
        )


async def test_stale_v13_inconclusive_hold_becomes_no_fault_timeout(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    agent_id, attempt_id, quarantine_id = await _seed_hold(
        session_maker,
        extra_attempts=2,
        extra_workers=("5OtherWorkerAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",),
    )

    counters = await finalize_review_timeouts(session_maker)

    assert counters == {"candidates": 1, "finalized": 1, "skipped": 0}
    async with session_maker() as session:
        agent = await session.get(Agent, agent_id)
        quarantine = await session.get(ScreeningQuarantine, quarantine_id)
        grant = await session.scalar(
            select(ScreeningRetryOverride).where(
                ScreeningRetryOverride.attempt_id == attempt_id
            )
        )
        resolutions = list(
            await session.scalars(
                select(ScreeningQuarantineResolution).where(
                    ScreeningQuarantineResolution.quarantine_id == quarantine_id
                )
            )
        )
    assert agent is not None and quarantine is not None and grant is not None
    # Never a plain REJECT, never a ban: parked for the automatic retry.
    assert agent.status == AgentStatus.SCREENING_FAILED
    assert agent.screening_reason == REVIEW_TIMED_OUT_PUBLIC_REASON
    assert agent.screening_reason_code == REVIEW_TIMED_OUT_REASON_CODE
    assert quarantine.status == "resolved"
    assert quarantine.resolution == "rescreen"
    assert quarantine.resolved_by == REVIEW_TIMEOUT_FINALIZER_ACTOR
    assert [row.actor for row in resolutions] == [REVIEW_TIMEOUT_FINALIZER_ACTOR]
    # The no-fault retry grant is bound to the exact held artifact.
    assert grant.actor == REVIEW_TIMEOUT_FINALIZER_ACTOR
    assert grant.artifact_sha256 == agent.sha256
    assert grant.expected_score_count == 0
    assert grant.force_full_review is False

    (record,) = await _decisions(session_maker, agent_id)
    assert record.outcome == REVIEW_TIMED_OUT_OUTCOME
    assert record.violation_proven is False
    assert record.precedent_weight is False
    assert record.failure_domain == "platform"
    assert record.reason_codes == [
        REVIEW_TIMED_OUT_REASON_CODE,
        V2_PLATFORM_VERIFICATION_FAILED,
        "source-review-inconclusive",
    ]
    assert record.retry_count == 3
    assert record.independent_workers == 2
    assert record.retry_grant_id == grant.override_id
    assert record.quarantine_id == quarantine_id
    assert record.attempt_id == attempt_id
    assert record.policy_version == STRICT_TWO_OUTCOME_POLICY_VERSION
    assert record.identities["artifact_sha256"] == agent.sha256
    assert record.identities["submission_uuid"] == str(agent_id)
    assert record.failed_checks == ["source-review-inconclusive"]
    assert record.public_reason == REVIEW_TIMED_OUT_PUBLIC_REASON
    assert record.reviewer == REVIEW_TIMEOUT_FINALIZER_ACTOR
    assert record.supersedes_decision is None

    # Idempotent: a second pass finds nothing left to finalize.
    assert await finalize_review_timeouts(session_maker) == {
        "candidates": 0,
        "finalized": 0,
        "skipped": 0,
    }


async def test_timed_out_submission_is_claimable_again_ahead_of_newer_work(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """The no-fault grant is what re-admits the parked agent to the queue."""
    agent_id, _attempt_id, _quarantine_id = await _seed_hold(session_maker)
    update_effective_screener_policy(
        STRICT_TWO_OUTCOME_POLICY_VERSION, rescreen_scored=False
    )
    try:
        await finalize_review_timeouts(session_maker)
        async with session_maker() as session, session.begin():
            claimed = await claim_screening_attempts(
                session,
                screener_hotkey=_SCREENER,
                now=datetime.now(UTC),
                ttl=timedelta(minutes=70),
                limit=5,
            )
        assert [agent.agent_id for agent, _attempt, _dup in claimed] == [agent_id]
    finally:
        update_effective_screener_policy(
            SCREENING_FLOOR_POLICY_VERSION, rescreen_scored=False
        )


async def test_provider_recorded_on_attempt_yields_v3_provider_domain(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    agent_id, _attempt_id, _quarantine_id = await _seed_hold(
        session_maker, reason_code="challenge-inconclusive", failure_provider="targon"
    )

    await finalize_review_timeouts(session_maker)

    (record,) = await _decisions(session_maker, agent_id)
    assert record.failure_domain == "provider"
    assert V3_PROVIDER_VERIFICATION_FAILED in record.reason_codes
    assert record.violation_proven is False


@pytest.mark.parametrize(
    ("kwargs", "why"),
    [
        (
            {"age": _WINDOW - timedelta(hours=1)},
            "inside the published verification window",
        ),
        (
            {"policy_version": STRICT_TWO_OUTCOME_POLICY_VERSION - 1},
            "older-policy hold keeps its signed compatibility behaviour",
        ),
        (
            {
                "reason_code": "agentic-source-review-tripwire",
                "finding_digest": "b" * 64,
                "finding": {"risk": "high", "summary": "finding"},
            },
            "a hold with a finding is an operator decision, not a processing state",
        ),
        (
            {
                "reason_code": "source-review-inconclusive",
                "finding_digest": "b" * 64,
                "finding": {"risk": "high", "summary": "finding"},
            },
            "a bound finding digest is never overridden by the deadline",
        ),
    ],
)
async def test_finalizer_leaves_non_qualifying_holds_alone(
    session_maker: async_sessionmaker[AsyncSession],
    kwargs: dict,
    why: str,
) -> None:
    agent_id, _attempt_id, quarantine_id = await _seed_hold(session_maker, **kwargs)

    counters = await finalize_review_timeouts(session_maker)

    assert counters["finalized"] == 0, why
    async with session_maker() as session:
        agent = await session.get(Agent, agent_id)
        quarantine = await session.get(ScreeningQuarantine, quarantine_id)
    assert agent is not None and agent.status == AgentStatus.QUARANTINED, why
    assert quarantine is not None and quarantine.status == "active", why
    assert await _decisions(session_maker, agent_id) == []


async def test_every_published_non_decisive_code_is_selected(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """The finalizer's selection covers the whole policy-v13 non-decisive set."""
    expected: set[UUID] = set()
    for code in sorted(NON_DECISIVE_REASON_CODES):
        _agent, _attempt, quarantine_id = await _seed_hold(
            session_maker, reason_code=code
        )
        expected.add(quarantine_id)
    async with session_maker() as session:
        selected = await select_timed_out_quarantines(
            session, now=datetime.now(UTC), limit=100
        )
    assert set(selected) == expected


async def test_concurrent_operator_resolution_wins(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """A row resolved between selection and the locked re-check is skipped."""
    agent_id, _attempt_id, quarantine_id = await _seed_hold(session_maker)
    async with session_maker() as session:
        candidates = await select_timed_out_quarantines(session, now=datetime.now(UTC))
    assert candidates == [quarantine_id]
    async with session_maker() as session, session.begin():
        quarantine = await session.get(ScreeningQuarantine, quarantine_id)
        agent = await session.get(Agent, agent_id)
        assert quarantine is not None and agent is not None
        quarantine.status = "resolved"
        quarantine.resolved_at = datetime.now(UTC)
        quarantine.resolved_by = "operator"
        quarantine.resolution = "release"
        quarantine.resolution_reason = "operator released it first"
        agent.status = AgentStatus.EVALUATING

    counters = await finalize_review_timeouts(session_maker)

    assert counters["finalized"] == 0
    async with session_maker() as session:
        agent = await session.get(Agent, agent_id)
    assert agent is not None and agent.status == AgentStatus.EVALUATING
    assert await _decisions(session_maker, agent_id) == []


async def test_paused_scored_canary_is_rearmed_and_keeps_its_board_row(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """A hold on a scored policy canary pauses the release, not the agent."""
    agent_id, attempt_id, quarantine_id = await _seed_hold(
        session_maker, agent_status=AgentStatus.SCORED
    )
    async with session_maker() as session, session.begin():
        session.add(
            ScreenerPolicyActivation(
                revision=1,
                parent_revision=0,
                target_policy_version=STRICT_TWO_OUTCOME_POLICY_VERSION,
                activate_at=datetime.now(UTC) - timedelta(days=2),
                rescreen_scored=True,
                canary_only=False,
                reason="v13 activation with scored rescreen",
                actor="operator",
                created_at=datetime.now(UTC) - timedelta(days=3),
            )
        )
        await session.flush()
        session.add(
            ScoredPolicyRescreenRelease(
                release_id=uuid4(),
                activation_revision=1,
                target_policy_version=STRICT_TWO_OUTCOME_POLICY_VERSION,
                agent_id=agent_id,
                position=1,
                state="paused",
                attempt_id=attempt_id,
                actor="operator",
                reason="release position 1",
                created_at=datetime.now(UTC) - timedelta(days=2),
                updated_at=datetime.now(UTC) - timedelta(days=1),
            )
        )

    counters = await finalize_review_timeouts(session_maker)

    assert counters["finalized"] == 1
    async with session_maker() as session:
        agent = await session.get(Agent, agent_id)
        release = await session.scalar(
            select(ScoredPolicyRescreenRelease).where(
                ScoredPolicyRescreenRelease.agent_id == agent_id
            )
        )
        quarantine = await session.get(ScreeningQuarantine, quarantine_id)
    assert agent is not None and agent.status == AgentStatus.SCORED
    assert release is not None and release.state == "pending"
    assert release.attempt_id is None
    assert release.actor == REVIEW_TIMEOUT_FINALIZER_ACTOR
    assert quarantine is not None and quarantine.status == "resolved"
    (record,) = await _decisions(session_maker, agent_id)
    assert record.outcome == REVIEW_TIMED_OUT_OUTCOME


async def test_loop_tick_reports_counters_and_stops_cleanly(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_hold(session_maker)
    finalizer = ReviewTimeoutFinalizer(
        session_maker=session_maker, interval_seconds=3600
    )
    await finalizer.start()
    try:
        counters = await finalizer.tick()
    finally:
        await finalizer.aclose()
    assert counters["finalized"] == 1
