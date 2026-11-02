"""ATH-hold and copy-review queue-age SLO against a real Postgres (#2042).

These cover the slice 2 acceptance criteria. A deep-review retry does not
reset the hold clock, and a reopen does restart it. A deep pass started
before the hold opened is superseded and does not decide the reason.
Stranded and terminal holds stay visible as ghosts and never count as
actionable. The tests also cover a mixed queue across classes, resolution
throughput, and unset thresholds reporting null. The pure math lives in
``test_ath_review_queue_slo_math.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.db.models import Agent, AgentStatus, AthReview, ScreeningAttempt
from ditto.db.queries.ath_review_queue_slo import (
    AthQueueClass,
    QueueAgeThresholds,
    load_agent_ath_review_state,
    load_ath_review_queue_slo_snapshot,
)

pytestmark = pytest.mark.asyncio

_SHA256 = "a" * 64
_HOTKEY = "5" + "F" * 47


def _agent(
    *,
    status: AgentStatus = AgentStatus.ATH_PENDING_REVIEW,
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


def _review(
    *,
    agent_id: UUID,
    opened_at: datetime,
    provenance: dict[str, Any] | None = None,
    reopened_at: datetime | None = None,
    resolved_at: datetime | None = None,
) -> AthReview:
    resolved = resolved_at is not None
    return AthReview(
        review_id=uuid4(),
        agent_id=agent_id,
        status="resolved" if resolved else "pending",
        opened_at=opened_at,
        reopened_at=reopened_at,
        resolved_at=resolved_at,
        resolved_by="operator@example.com" if resolved else None,
        resolution="clear" if resolved else None,
        resolution_reason="cleared on review" if resolved else None,
        original_reason="held for review",
        original_policy_version=8,
        original_evidence={},
        algorithm_provenance=provenance if provenance is not None else {},
    )


def _attempt(*, agent_id: UUID, status: str, started_at: datetime) -> ScreeningAttempt:
    return ScreeningAttempt(
        attempt_id=uuid4(),
        agent_id=agent_id,
        screener_hotkey=_HOTKEY,
        policy_version=13,
        status=status,
        started_at=started_at,
        deadline=started_at + timedelta(hours=2),
        finished_at=None if status == "running" else started_at + timedelta(minutes=5),
    )


_DEFERRED = {"review_kind": "deferred_source_review"}
_DOUBLE_CHECK = {
    "review_kind": "deferred_source_review",
    "trigger": "integrity_double_check",
}


class TestEmptyQueue:
    async def test_empty_queue_reports_null_ages_and_every_class(
        self, session: AsyncSession
    ) -> None:
        snapshot = await load_ath_review_queue_slo_snapshot(session)
        assert snapshot.ath.backlog_count == 0
        assert snapshot.ath.p50_age_seconds is None
        assert snapshot.ath.p95_age_seconds is None
        assert snapshot.ath.oldest_age_seconds is None
        assert snapshot.ath.overdue_count is None
        assert snapshot.copy.backlog_count == 0
        assert len(snapshot.classes) == 5
        assert snapshot.ghost_count == 0


class TestClockAndRetry:
    async def test_deep_review_retry_does_not_reset_the_hold_clock(
        self, session: AsyncSession
    ) -> None:
        now = datetime.now(UTC)
        agent_id = uuid4()
        async with session.begin():
            session.add(_agent(created_at=now - timedelta(days=2), agent_id=agent_id))
            await session.flush()
            session.add(
                _review(
                    agent_id=agent_id,
                    opened_at=now - timedelta(hours=5),
                    provenance=_DEFERRED,
                )
            )
            session.add(
                _attempt(
                    agent_id=agent_id,
                    status="expired",
                    started_at=now - timedelta(hours=4),
                )
            )
            session.add(
                _attempt(
                    agent_id=agent_id,
                    status="running",
                    started_at=now - timedelta(seconds=30),
                )
            )

        snapshot = await load_ath_review_queue_slo_snapshot(session)
        stats = snapshot.for_class("deferred_source_review")
        assert stats.backlog_count == 1
        assert stats.active_work_count == 1
        assert stats.oldest_age_seconds is not None
        assert abs(stats.oldest_age_seconds - 5 * 3600) < 120

        state = await load_agent_ath_review_state(session, agent_id=agent_id)
        assert state is not None
        assert state.reason == "active_work"
        assert abs(state.age_seconds - 5 * 3600) < 120

    async def test_reopen_restarts_the_clock(self, session: AsyncSession) -> None:
        now = datetime.now(UTC)
        agent_id = uuid4()
        async with session.begin():
            session.add(_agent(created_at=now - timedelta(days=9), agent_id=agent_id))
            await session.flush()
            session.add(
                _review(
                    agent_id=agent_id,
                    opened_at=now - timedelta(days=7),
                    reopened_at=now - timedelta(hours=1),
                    provenance={"review_kind": "copy"},
                )
            )

        snapshot = await load_ath_review_queue_slo_snapshot(session)
        assert snapshot.copy.oldest_age_seconds is not None
        assert abs(snapshot.copy.oldest_age_seconds - 3600) < 120

    async def test_attempt_before_the_hold_is_superseded(
        self, session: AsyncSession
    ) -> None:
        """The pre-score screen that passed before the hold opened is not the
        deep pass. The hold is still waiting on screener capacity."""
        now = datetime.now(UTC)
        agent_id = uuid4()
        async with session.begin():
            session.add(_agent(created_at=now - timedelta(days=1), agent_id=agent_id))
            await session.flush()
            session.add(
                _attempt(
                    agent_id=agent_id,
                    status="passed",
                    started_at=now - timedelta(hours=20),
                )
            )
            session.add(
                _review(
                    agent_id=agent_id,
                    opened_at=now - timedelta(hours=2),
                    provenance=_DOUBLE_CHECK,
                )
            )

        snapshot = await load_ath_review_queue_slo_snapshot(session)
        stats = snapshot.for_class("integrity_double_check")
        assert stats.backlog_count == 1
        assert stats.capacity_wait_count == 1
        assert stats.escalation_count == 0

    async def test_concluded_deep_pass_hands_off_to_an_operator(
        self, session: AsyncSession
    ) -> None:
        now = datetime.now(UTC)
        backoff_id, escalated_id = uuid4(), uuid4()
        async with session.begin():
            for agent_id in (backoff_id, escalated_id):
                session.add(
                    _agent(created_at=now - timedelta(days=1), agent_id=agent_id)
                )
            await session.flush()
            for agent_id in (backoff_id, escalated_id):
                session.add(
                    _review(
                        agent_id=agent_id,
                        opened_at=now - timedelta(hours=3),
                        provenance=_DEFERRED,
                    )
                )
            session.add(
                _attempt(
                    agent_id=backoff_id,
                    status="failed",
                    started_at=now - timedelta(hours=2),
                )
            )
            session.add(
                _attempt(
                    agent_id=escalated_id,
                    status="rejected",
                    started_at=now - timedelta(hours=2),
                )
            )

        stats = (await load_ath_review_queue_slo_snapshot(session)).for_class(
            "deferred_source_review"
        )
        assert stats.infrastructure_backoff_count == 1
        assert stats.escalation_count == 1


class TestGhosts:
    async def test_stranded_and_terminal_holds_are_ghosts_not_backlog(
        self, session: AsyncSession
    ) -> None:
        now = datetime.now(UTC)
        live_id, terminal_id, stranded_id, orphan_id = (uuid4() for _ in range(4))
        async with session.begin():
            session.add(_agent(created_at=now, agent_id=live_id))
            session.add(
                _agent(
                    status=AgentStatus.BANNED,
                    created_at=now - timedelta(days=30),
                    agent_id=terminal_id,
                )
            )
            session.add(
                _agent(
                    status=AgentStatus.SCORED,
                    created_at=now - timedelta(days=20),
                    agent_id=stranded_id,
                )
            )
            session.add(_agent(created_at=now, agent_id=orphan_id))
            await session.flush()
            session.add(
                _review(agent_id=live_id, opened_at=now - timedelta(minutes=10))
            )
            session.add(
                _review(agent_id=terminal_id, opened_at=now - timedelta(days=29))
            )
            session.add(
                _review(agent_id=stranded_id, opened_at=now - timedelta(days=19))
            )

        snapshot = await load_ath_review_queue_slo_snapshot(
            session,
            ath_thresholds=QueueAgeThresholds(max_actionable_age_seconds=3600),
        )
        assert snapshot.ath.backlog_count == 1
        assert snapshot.ath.oldest_agent_id == live_id
        assert snapshot.ath.oldest_age_seconds is not None
        assert snapshot.ath.oldest_age_seconds < 3600
        assert snapshot.ath.overdue_count == 0
        assert snapshot.stranded_terminal_ghost_count == 1
        assert snapshot.stranded_hold_ghost_count == 1
        assert snapshot.held_without_review_ghost_count == 1
        assert snapshot.ghost_count == 3

        for ghost_id in (terminal_id, stranded_id, orphan_id):
            assert await load_agent_ath_review_state(session, agent_id=ghost_id) is None


class TestMixedQueue:
    async def test_classes_aggregate_and_copy_has_its_own_thresholds(
        self, session: AsyncSession
    ) -> None:
        now = datetime.now(UTC)
        legacy, copy_id, overfit, anomaly, double_check = (uuid4() for _ in range(5))
        resolved_copy, resolved_old = uuid4(), uuid4()
        specs: list[tuple[UUID, timedelta, dict[str, Any]]] = [
            (legacy, timedelta(hours=10), {"reference_provenance": "legacy"}),
            (copy_id, timedelta(hours=2), {"review_kind": "copy"}),
            (overfit, timedelta(hours=4), {"review_kind": "benchmark_overfit"}),
            (anomaly, timedelta(hours=6), {"review_kind": "anomalous_score"}),
            (double_check, timedelta(hours=8), _DOUBLE_CHECK),
        ]
        async with session.begin():
            for agent_id, _, _ in specs:
                session.add(
                    _agent(created_at=now - timedelta(days=2), agent_id=agent_id)
                )
            for agent_id in (resolved_copy, resolved_old):
                session.add(
                    _agent(
                        status=AgentStatus.SCORED,
                        created_at=now - timedelta(days=5),
                        agent_id=agent_id,
                    )
                )
            await session.flush()
            for agent_id, age, provenance in specs:
                session.add(
                    _review(
                        agent_id=agent_id,
                        opened_at=now - age,
                        provenance=provenance,
                    )
                )
            session.add(
                _review(
                    agent_id=resolved_copy,
                    opened_at=now - timedelta(days=1),
                    resolved_at=now - timedelta(hours=1),
                )
            )
            # Outside the 24h throughput window.
            session.add(
                _review(
                    agent_id=resolved_old,
                    opened_at=now - timedelta(days=4),
                    resolved_at=now - timedelta(days=3),
                )
            )

        snapshot = await load_ath_review_queue_slo_snapshot(
            session,
            ath_thresholds=QueueAgeThresholds(
                max_actionable_age_seconds=5 * 3600, p95_age_seconds=3600
            ),
            copy_thresholds=QueueAgeThresholds(max_actionable_age_seconds=3 * 3600),
        )

        assert snapshot.ath.backlog_count == 5
        # 6h, 8h, and 10h holds are past 5h.
        assert snapshot.ath.overdue_count == 3
        assert snapshot.ath.p95_exceeds_threshold is True
        assert snapshot.ath.oldest_agent_id == legacy
        assert snapshot.ath.throughput_completed_count == 1
        assert snapshot.ath.escalation_count == 4
        assert snapshot.ath.capacity_wait_count == 1

        copy = snapshot.copy
        # The legacy hold with no review_kind is copy review.
        assert copy.backlog_count == 2
        assert copy.overdue_count == 1
        assert copy.throughput_completed_count == 1
        assert copy.oldest_agent_id == legacy

        single_hold_classes: tuple[AthQueueClass, ...] = (
            "benchmark_overfit",
            "anomalous_score",
            "integrity_double_check",
        )
        for queue_class in single_hold_classes:
            stats = snapshot.for_class(queue_class)
            assert stats.backlog_count == 1
            assert stats.overdue_count is None
        assert snapshot.for_class("deferred_source_review").backlog_count == 0
