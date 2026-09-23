"""Ordinary source-review queue-age SLO: clock, reason, and ghost predicates.

Covers ditto-subnet#2042 vertical slice 1 acceptance: retry (the clock
reflects the CURRENT attempt), supersession (an old attempt neither counts
nor extends the clock), terminal-ghost exclusion (a stale-looking active row
whose agent already moved on is visible separately, never actionable),
resolved-quarantine reconciliation, mixed-queue behavior, p50/p95/oldest-age/
throughput correctness on a constructed distribution, and an unset threshold
reporting null rather than zero or "healthy".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.db.models import Agent, AgentStatus, ScreeningAttempt, ScreeningQuarantine
from ditto.db.queries.source_review_queue_slo import (
    load_agent_ordinary_review_state,
    load_source_review_queue_slo_snapshot,
)

pytestmark = pytest.mark.asyncio

_SHA256 = "a" * 64
_HOTKEY = "5" + "F" * 47


def _agent(
    *,
    status: AgentStatus,
    created_at: datetime,
    agent_id: UUID | None = None,
) -> Agent:
    return Agent(
        agent_id=agent_id or uuid4(),
        miner_hotkey=_HOTKEY,
        name="agent",
        sha256=_SHA256,
        status=status,
        created_at=created_at,
    )


def _attempt(
    *,
    agent_id: UUID,
    status: str,
    started_at: datetime,
    finished_at: datetime | None = None,
    build_only: bool = False,
    attempt_id: UUID | None = None,
) -> ScreeningAttempt:
    return ScreeningAttempt(
        attempt_id=attempt_id or uuid4(),
        agent_id=agent_id,
        screener_hotkey=_HOTKEY,
        policy_version=13,
        status=status,
        started_at=started_at,
        deadline=started_at + timedelta(hours=2),
        finished_at=finished_at,
        build_only=build_only,
    )


def _quarantine(
    *,
    agent_id: UUID,
    attempt_id: UUID,
    status: str = "active",
    resolved_at: datetime | None = None,
) -> ScreeningQuarantine:
    return ScreeningQuarantine(
        quarantine_id=uuid4(),
        agent_id=agent_id,
        attempt_id=attempt_id,
        screener_hotkey=_HOTKEY,
        policy_version=13,
        manifest_digest="b" * 64,
        reason_code="source-review-inconclusive",
        status=status,
        resolved_at=resolved_at,
        resolved_by="operator@example.com" if status == "resolved" else None,
        resolution="release" if status == "resolved" else None,
        resolution_reason="cleared on review" if status == "resolved" else None,
    )


class TestEmptyQueue:
    async def test_empty_backlog_reports_null_percentiles_not_zero(
        self, session: AsyncSession
    ) -> None:
        snapshot = await load_source_review_queue_slo_snapshot(session)
        assert snapshot.backlog_count == 0
        assert snapshot.p50_age_seconds is None
        assert snapshot.p95_age_seconds is None
        assert snapshot.oldest_age_seconds is None
        assert snapshot.ghost_count == 0
        assert snapshot.throughput_completed_count == 0
        assert snapshot.throughput_per_hour == 0.0


class TestUnsetThreshold:
    async def test_unset_threshold_reports_null_not_zero_or_healthy(
        self, session: AsyncSession
    ) -> None:
        now = datetime.now(UTC)
        async with session.begin():
            session.add(_agent(status=AgentStatus.UPLOADED, created_at=now))

        # Default call: no threshold configured anywhere.
        snapshot = await load_source_review_queue_slo_snapshot(session)
        assert snapshot.max_actionable_age_threshold_seconds is None
        assert snapshot.overdue_count is None
        assert snapshot.p95_age_threshold_seconds is None
        assert snapshot.p95_exceeds_threshold is None

        # A configured threshold turns the null into a real, computed count --
        # proving the null above was genuinely "unset", not a hidden zero.
        thresholded = await load_source_review_queue_slo_snapshot(
            session, max_actionable_age_threshold_seconds=10
        )
        assert thresholded.overdue_count is not None


class TestRetryAndSupersession:
    async def test_retry_clock_reflects_current_attempt_not_sum_of_attempts(
        self, session: AsyncSession
    ) -> None:
        now = datetime.now(UTC)
        agent_id = uuid4()
        old_attempt_id = uuid4()
        new_attempt_id = uuid4()
        async with session.begin():
            session.add(
                _agent(
                    status=AgentStatus.SCREENING,
                    created_at=now - timedelta(hours=5),
                    agent_id=agent_id,
                )
            )
            # A superseded attempt from hours ago: if the clock summed
            # attempts instead of using only the current one, the reported
            # age would be roughly 5 hours instead of ~30 seconds.
            session.add(
                _attempt(
                    agent_id=agent_id,
                    status="failed",
                    started_at=now - timedelta(hours=5),
                    finished_at=now - timedelta(hours=4),
                    attempt_id=old_attempt_id,
                )
            )
            session.add(
                _attempt(
                    agent_id=agent_id,
                    status="running",
                    started_at=now - timedelta(seconds=30),
                    attempt_id=new_attempt_id,
                )
            )

        snapshot = await load_source_review_queue_slo_snapshot(session)
        assert snapshot.backlog_count == 1
        assert snapshot.active_work_count == 1
        assert snapshot.infrastructure_backoff_count == 0
        # The reported oldest age must reflect the CURRENT attempt (~30s),
        # not the sum of both attempts (~5h = 18_000s).
        assert snapshot.oldest_age_seconds is not None
        assert snapshot.oldest_age_seconds < 120

        agent_state = await load_agent_ordinary_review_state(session, agent_id=agent_id)
        assert agent_state is not None
        assert agent_state.reason == "active_work"
        assert agent_state.age_seconds < 120

    async def test_superseded_attempt_is_not_double_counted(
        self, session: AsyncSession
    ) -> None:
        now = datetime.now(UTC)
        agent_id = uuid4()
        async with session.begin():
            session.add(
                _agent(
                    status=AgentStatus.SCREENING_FAILED,
                    created_at=now - timedelta(hours=3),
                    agent_id=agent_id,
                )
            )
            # Two superseded, non-current attempts plus the current one: the
            # backlog must still count this agent exactly once.
            for offset_hours in (3, 2, 1):
                session.add(
                    _attempt(
                        agent_id=agent_id,
                        status="failed",
                        started_at=now - timedelta(hours=offset_hours),
                        finished_at=now - timedelta(hours=offset_hours - 0.5),
                    )
                )

        snapshot = await load_source_review_queue_slo_snapshot(session)
        assert snapshot.backlog_count == 1
        assert snapshot.infrastructure_backoff_count == 1


class TestGhostReconciliation:
    async def test_stale_running_attempt_excluded_but_counted_as_ghost(
        self, session: AsyncSession
    ) -> None:
        now = datetime.now(UTC)
        agent_id = uuid4()
        async with session.begin():
            # Agent already progressed past screening (#2038's target bug: a
            # screening_attempts row that never got closed out).
            session.add(
                _agent(
                    status=AgentStatus.EVALUATING,
                    created_at=now - timedelta(hours=1),
                    agent_id=agent_id,
                )
            )
            session.add(
                _attempt(
                    agent_id=agent_id,
                    status="running",
                    started_at=now - timedelta(hours=1),
                )
            )

        snapshot = await load_source_review_queue_slo_snapshot(session)
        assert snapshot.backlog_count == 0
        assert snapshot.active_work_count == 0
        assert snapshot.stale_running_ghost_count == 1
        assert snapshot.resolved_quarantine_ghost_count == 0
        assert snapshot.ghost_count == 1

        # A ghost is not shown to a miner as an active review either.
        agent_state = await load_agent_ordinary_review_state(session, agent_id=agent_id)
        assert agent_state is None

    @pytest.mark.parametrize(
        "terminal_status",
        [
            AgentStatus.REJECTED,
            AgentStatus.BANNED,
            AgentStatus.SCORED,
            AgentStatus.LIVE,
        ],
    )
    async def test_every_terminal_or_progressed_status_counts_as_ghost(
        self, session: AsyncSession, terminal_status: AgentStatus
    ) -> None:
        now = datetime.now(UTC)
        agent_id = uuid4()
        async with session.begin():
            session.add(
                _agent(status=terminal_status, created_at=now, agent_id=agent_id)
            )
            session.add(_attempt(agent_id=agent_id, status="running", started_at=now))
        snapshot = await load_source_review_queue_slo_snapshot(session)
        assert snapshot.backlog_count == 0
        assert snapshot.stale_running_ghost_count == 1

    async def test_resolved_quarantine_excluded_but_counted_as_ghost(
        self, session: AsyncSession
    ) -> None:
        now = datetime.now(UTC)
        agent_id = uuid4()
        attempt_id = uuid4()
        async with session.begin():
            # Agent still shows quarantined although its quarantine already
            # resolved -- the reconciliation gap Peyton named explicitly.
            session.add(
                _agent(
                    status=AgentStatus.QUARANTINED,
                    created_at=now - timedelta(hours=2),
                    agent_id=agent_id,
                )
            )
            session.add(
                _attempt(
                    agent_id=agent_id,
                    status="quarantined",
                    started_at=now - timedelta(hours=2),
                    attempt_id=attempt_id,
                )
            )
            session.add(
                _quarantine(
                    agent_id=agent_id,
                    attempt_id=attempt_id,
                    status="resolved",
                    resolved_at=now - timedelta(hours=1),
                )
            )

        snapshot = await load_source_review_queue_slo_snapshot(session)
        assert snapshot.backlog_count == 0
        assert snapshot.escalation_count == 0
        assert snapshot.resolved_quarantine_ghost_count == 1
        assert snapshot.stale_running_ghost_count == 0
        assert snapshot.ghost_count == 1

        agent_state = await load_agent_ordinary_review_state(session, agent_id=agent_id)
        assert agent_state is None

    async def test_active_quarantine_is_escalation_not_a_ghost(
        self, session: AsyncSession
    ) -> None:
        now = datetime.now(UTC)
        agent_id = uuid4()
        attempt_id = uuid4()
        async with session.begin():
            session.add(
                _agent(
                    status=AgentStatus.QUARANTINED,
                    created_at=now - timedelta(minutes=90),
                    agent_id=agent_id,
                )
            )
            session.add(
                _attempt(
                    agent_id=agent_id,
                    status="quarantined",
                    started_at=now - timedelta(minutes=90),
                    attempt_id=attempt_id,
                )
            )
            session.add(_quarantine(agent_id=agent_id, attempt_id=attempt_id))

        snapshot = await load_source_review_queue_slo_snapshot(session)
        assert snapshot.backlog_count == 1
        assert snapshot.escalation_count == 1
        assert snapshot.ghost_count == 0

        agent_state = await load_agent_ordinary_review_state(session, agent_id=agent_id)
        assert agent_state is not None
        assert agent_state.reason == "escalation"


class TestMixedQueue:
    async def test_healthy_overdue_and_ghost_rows_coexist_and_each_reports_correctly(
        self, session: AsyncSession
    ) -> None:
        now = datetime.now(UTC)
        uploaded_id, screening_id, failed_id, quarantined_id = (
            uuid4(),
            uuid4(),
            uuid4(),
            uuid4(),
        )
        ghost_running_id, ghost_quarantine_id = uuid4(), uuid4()
        quarantine_attempt_id = uuid4()
        ghost_quarantine_attempt_id = uuid4()
        async with session.begin():
            # capacity_wait: never claimed.
            session.add(
                _agent(
                    status=AgentStatus.UPLOADED,
                    created_at=now - timedelta(seconds=10),
                    agent_id=uploaded_id,
                )
            )
            # active_work: claimed and running.
            session.add(
                _agent(
                    status=AgentStatus.SCREENING,
                    created_at=now - timedelta(minutes=5),
                    agent_id=screening_id,
                )
            )
            session.add(
                _attempt(
                    agent_id=screening_id,
                    status="running",
                    started_at=now - timedelta(minutes=5),
                )
            )
            # infrastructure_backoff, deliberately old enough to be overdue
            # once a threshold is configured.
            session.add(
                _agent(
                    status=AgentStatus.SCREENING_FAILED,
                    created_at=now - timedelta(hours=10),
                    agent_id=failed_id,
                )
            )
            session.add(
                _attempt(
                    agent_id=failed_id,
                    status="failed",
                    started_at=now - timedelta(hours=10),
                    finished_at=now - timedelta(hours=9),
                )
            )
            # escalation: active quarantine hold, comfortably under the
            # threshold so it is never ambiguous with the overdue check below.
            session.add(
                _agent(
                    status=AgentStatus.QUARANTINED,
                    created_at=now - timedelta(minutes=20),
                    agent_id=quarantined_id,
                )
            )
            session.add(
                _attempt(
                    agent_id=quarantined_id,
                    status="quarantined",
                    started_at=now - timedelta(minutes=20),
                    attempt_id=quarantine_attempt_id,
                )
            )
            session.add(
                _quarantine(agent_id=quarantined_id, attempt_id=quarantine_attempt_id)
            )
            # ghost: stale running attempt on a progressed agent.
            session.add(
                _agent(
                    status=AgentStatus.LIVE,
                    created_at=now - timedelta(days=1),
                    agent_id=ghost_running_id,
                )
            )
            session.add(
                _attempt(
                    agent_id=ghost_running_id,
                    status="running",
                    started_at=now - timedelta(days=1),
                )
            )
            # ghost: resolved-quarantine reconciliation gap.
            session.add(
                _agent(
                    status=AgentStatus.QUARANTINED,
                    created_at=now - timedelta(days=2),
                    agent_id=ghost_quarantine_id,
                )
            )
            session.add(
                _attempt(
                    agent_id=ghost_quarantine_id,
                    status="quarantined",
                    started_at=now - timedelta(days=2),
                    attempt_id=ghost_quarantine_attempt_id,
                )
            )
            session.add(
                _quarantine(
                    agent_id=ghost_quarantine_id,
                    attempt_id=ghost_quarantine_attempt_id,
                    status="resolved",
                    resolved_at=now - timedelta(days=1),
                )
            )

        snapshot = await load_source_review_queue_slo_snapshot(
            session, max_actionable_age_threshold_seconds=3600
        )
        assert snapshot.backlog_count == 4
        assert snapshot.capacity_wait_count == 1
        assert snapshot.active_work_count == 1
        assert snapshot.infrastructure_backoff_count == 1
        assert snapshot.escalation_count == 1
        assert snapshot.stale_running_ghost_count == 1
        assert snapshot.resolved_quarantine_ghost_count == 1
        assert snapshot.ghost_count == 2
        # Only the ~10h-old infrastructure_backoff row clears a 1h threshold;
        # every other row is comfortably under it, so exactly one is overdue.
        assert snapshot.overdue_count == 1


class TestDistributionCorrectness:
    async def test_p50_p95_oldest_age_and_throughput_on_a_constructed_distribution(
        self, session: AsyncSession
    ) -> None:
        now = datetime.now(UTC)
        ages_seconds = (1000, 2000, 3000, 4000)
        async with session.begin():
            for age in ages_seconds:
                agent_id = uuid4()
                session.add(
                    _agent(
                        status=AgentStatus.SCREENING,
                        created_at=now - timedelta(seconds=age + 60),
                        agent_id=agent_id,
                    )
                )
                session.add(
                    _attempt(
                        agent_id=agent_id,
                        status="running",
                        started_at=now - timedelta(seconds=age),
                    )
                )
            # Throughput: two completed, non-build-only attempts inside the
            # 24h window, one build-only (excluded), one outside the window
            # (excluded).
            in_window_agent = uuid4()
            session.add(
                _agent(
                    status=AgentStatus.EVALUATING,
                    created_at=now,
                    agent_id=in_window_agent,
                )
            )
            session.add(
                _attempt(
                    agent_id=in_window_agent,
                    status="passed",
                    started_at=now - timedelta(hours=2),
                    finished_at=now - timedelta(hours=1),
                )
            )
            rejected_agent = uuid4()
            session.add(
                _agent(
                    status=AgentStatus.REJECTED, created_at=now, agent_id=rejected_agent
                )
            )
            session.add(
                _attempt(
                    agent_id=rejected_agent,
                    status="rejected",
                    started_at=now - timedelta(hours=3),
                    finished_at=now - timedelta(hours=2),
                )
            )
            build_only_agent = uuid4()
            session.add(
                _agent(
                    status=AgentStatus.EVALUATING,
                    created_at=now,
                    agent_id=build_only_agent,
                )
            )
            session.add(
                _attempt(
                    agent_id=build_only_agent,
                    status="passed",
                    started_at=now - timedelta(hours=1),
                    finished_at=now - timedelta(minutes=30),
                    build_only=True,
                )
            )
            outside_window_agent = uuid4()
            session.add(
                _agent(
                    status=AgentStatus.REJECTED,
                    created_at=now,
                    agent_id=outside_window_agent,
                )
            )
            session.add(
                _attempt(
                    agent_id=outside_window_agent,
                    status="rejected",
                    started_at=now - timedelta(hours=30),
                    finished_at=now - timedelta(hours=26),
                )
            )

        snapshot = await load_source_review_queue_slo_snapshot(session)
        assert snapshot.backlog_count == 4
        # n=4 -> percentile_cont(0.5) interpolates at position 1.5 (between
        # 2000 and 3000) = 2500; percentile_cont(0.95) at position 2.85
        # (between 3000 and 4000) = 3850. Generous tolerance absorbs the gap
        # between fixture construction and query execution.
        assert snapshot.p50_age_seconds is not None
        assert abs(snapshot.p50_age_seconds - 2500) < 15
        assert snapshot.p95_age_seconds is not None
        assert abs(snapshot.p95_age_seconds - 3850) < 15
        assert snapshot.oldest_age_seconds is not None
        assert abs(snapshot.oldest_age_seconds - 4000) < 15

        assert snapshot.throughput_window_hours == 24
        assert snapshot.throughput_completed_count == 2
        assert snapshot.throughput_per_hour == pytest.approx(2 / 24)


class TestPublicProjectionSafety:
    async def test_agent_not_in_ordinary_review_reports_none(
        self, session: AsyncSession
    ) -> None:
        now = datetime.now(UTC)
        agent_id = uuid4()
        async with session.begin():
            session.add(
                _agent(status=AgentStatus.SCORED, created_at=now, agent_id=agent_id)
            )
        assert (
            await load_agent_ordinary_review_state(session, agent_id=agent_id) is None
        )

    async def test_unknown_agent_reports_none(self, session: AsyncSession) -> None:
        assert await load_agent_ordinary_review_state(session, agent_id=uuid4()) is None

    async def test_state_carries_only_reason_and_age_no_operator_detail(
        self, session: AsyncSession
    ) -> None:
        now = datetime.now(UTC)
        agent_id = uuid4()
        attempt_id = uuid4()
        async with session.begin():
            session.add(
                _agent(
                    status=AgentStatus.QUARANTINED,
                    created_at=now - timedelta(minutes=10),
                    agent_id=agent_id,
                )
            )
            session.add(
                _attempt(
                    agent_id=agent_id,
                    status="quarantined",
                    started_at=now - timedelta(minutes=10),
                    attempt_id=attempt_id,
                )
            )
            session.add(_quarantine(agent_id=agent_id, attempt_id=attempt_id))

        state = await load_agent_ordinary_review_state(session, agent_id=agent_id)
        assert state is not None
        # Exactly two public-safe fields -- no reason_code, no evidence, no
        # finding, no cross-agent data.
        assert set(vars(state)) == {"reason", "age_seconds"}
        assert state.reason == "escalation"


class Test2038CrossLink:
    async def test_ghost_rows_disagree_with_repaired_queue_until_2038(
        self, session: AsyncSession
    ) -> None:
        """Regression contract for ditto-subnet#2038.

        Today, nothing transactionally guarantees a screening_attempts row
        closes out when its agent moves on some other way -- that is exactly
        the drift #2038's transactional cleanup will close. This module's
        answer, until then, is to keep the actionable metric correct (the
        stale row never counts as backlog) while surfacing the drift as a
        separate, visible ghost count rather than erasing it. Once #2038
        lands, constructing this exact scenario should become impossible (or
        the sweep should immediately reconcile it), at which point
        ``stale_running_ghost_count`` for this fixture should drop to 0 --
        this test's assertion of ``== 1`` is the tripwire that will fail and
        point back here.
        """
        now = datetime.now(UTC)
        agent_id = uuid4()
        async with session.begin():
            session.add(
                _agent(status=AgentStatus.SCORED, created_at=now, agent_id=agent_id)
            )
            session.add(_attempt(agent_id=agent_id, status="running", started_at=now))

        snapshot = await load_source_review_queue_slo_snapshot(session)
        # The metric (this module) and a naively "repaired" queue (one that
        # trusted screening_attempts.status alone) disagree today: the naive
        # reading would still show this as an open attempt. That disagreement
        # is the whole reason ghost_count exists as a separate, visible field.
        assert snapshot.backlog_count == 0
        assert snapshot.stale_running_ghost_count == 1
