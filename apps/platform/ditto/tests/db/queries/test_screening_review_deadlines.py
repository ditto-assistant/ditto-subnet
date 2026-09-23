"""A policy recommendation alone cannot create a live v13 deadline."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.db.models import (
    Agent,
    ScreeningAttempt,
    ScreeningQuarantine,
    ScreeningReviewDeadlineActivation,
    ScreeningReviewWindow,
)
from ditto.db.queries.screening_review_deadlines import review_deadline_binding

pytestmark = pytest.mark.asyncio


async def test_deadline_requires_prior_activation_and_exact_attempt_sha(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    agent_id, attempt_id, quarantine_id = uuid4(), uuid4(), uuid4()
    artifact_sha = "a1" * 32
    async with session_maker() as session, session.begin():
        session.add(
            Agent(
                agent_id=agent_id,
                miner_hotkey="5DeadlineTestMiner",
                name="deadline-test",
                sha256=artifact_sha,
                status=AgentStatus.QUARANTINED,
                created_at=now - timedelta(hours=1),
            )
        )
        await session.flush()
        session.add(
            ScreeningAttempt(
                attempt_id=attempt_id,
                agent_id=agent_id,
                artifact_sha256=artifact_sha,
                screener_hotkey="5DeadlineTestScreener",
                policy_version=13,
                status="quarantined",
                started_at=now + timedelta(hours=2),
                deadline=now + timedelta(hours=3),
                finished_at=now + timedelta(hours=3),
            )
        )
        await session.flush()
        session.add(
            ScreeningQuarantine(
                quarantine_id=quarantine_id,
                agent_id=agent_id,
                attempt_id=attempt_id,
                screener_hotkey="5DeadlineTestScreener",
                policy_version=13,
                manifest_digest="b2" * 32,
                reason_code="source-review-inconclusive",
                status="active",
                created_at=now,
            )
        )

    async with session_maker() as session:
        assert (
            await review_deadline_binding(session, quarantine_id=quarantine_id) is None
        )

    async with session_maker() as session, session.begin():
        activation = ScreeningReviewDeadlineActivation(
            policy_version=13,
            policy_digest="b2" * 32,
            activate_at=now + timedelta(hours=1),
            window_seconds=3600,
            reason="future activation only",
            actor="test-operator",
        )
        session.add(activation)
        await session.flush()
        activation_revision = activation.revision

    async with session_maker() as session:
        assert (
            await review_deadline_binding(session, quarantine_id=quarantine_id) is None
        )

    # Only an explicit exact-artifact window produces a deadline. The test
    # uses future timestamps to model a first claim after activation.
    async with session_maker() as session, session.begin():
        quarantine = await session.get(ScreeningQuarantine, quarantine_id)
        assert quarantine is not None
        quarantine.created_at = now + timedelta(hours=3)
        session.add(
            ScreeningReviewWindow(
                window_id=uuid4(),
                agent_id=agent_id,
                first_attempt_id=attempt_id,
                activation_revision=activation_revision,
                artifact_sha256=artifact_sha,
                policy_version=13,
                manifest_digest="b2" * 32,
                start_event="first-policy-claim",
                started_at=now + timedelta(hours=2),
                deadline_at=now + timedelta(hours=3),
            )
        )

    async with session_maker() as session:
        binding = await review_deadline_binding(session, quarantine_id=quarantine_id)
        assert binding is not None
        assert binding.artifact_sha256 == artifact_sha
        assert binding.policy_digest == "b2" * 32
        assert binding.deadline_at == now + timedelta(hours=3)

    # A later timestamp on the window must not relabel an older lease as the
    # post-activation first claim, even when SHA and policy still match.
    async with session_maker() as session, session.begin():
        first_attempt = await session.get(ScreeningAttempt, attempt_id)
        assert first_attempt is not None
        first_attempt.started_at = now - timedelta(minutes=30)
    async with session_maker() as session:
        assert (
            await review_deadline_binding(session, quarantine_id=quarantine_id) is None
        )
    async with session_maker() as session, session.begin():
        first_attempt = await session.get(ScreeningAttempt, attempt_id)
        assert first_attempt is not None
        first_attempt.started_at = now + timedelta(hours=2)

    # A retry cannot be presented as the first claim when an earlier pinned
    # lease exists for the same artifact and policy.
    earlier_id = uuid4()
    async with session_maker() as session, session.begin():
        session.add(
            ScreeningAttempt(
                attempt_id=earlier_id,
                agent_id=agent_id,
                artifact_sha256=artifact_sha,
                screener_hotkey="5DeadlineTestScreener",
                policy_version=13,
                status="expired",
                started_at=now + timedelta(hours=1, minutes=30),
                deadline=now + timedelta(hours=2),
                finished_at=now + timedelta(hours=2),
            )
        )
    async with session_maker() as session:
        assert (
            await review_deadline_binding(session, quarantine_id=quarantine_id) is None
        )
    async with session_maker() as session, session.begin():
        earlier = await session.get(ScreeningAttempt, earlier_id)
        assert earlier is not None
        await session.delete(earlier)

    async with session_maker() as session, session.begin():
        quarantine = await session.get(ScreeningQuarantine, quarantine_id)
        assert quarantine is not None
        quarantine.created_at = now + timedelta(hours=4)
    async with session_maker() as session:
        binding = await review_deadline_binding(session, quarantine_id=quarantine_id)
        assert binding is not None
        assert binding.deadline_at == now + timedelta(hours=3)

    # A same-version manifest/profile mismatch does not inherit the window.
    async with session_maker() as session, session.begin():
        quarantine = await session.get(ScreeningQuarantine, quarantine_id)
        assert quarantine is not None
        quarantine.manifest_digest = "c3" * 32
    async with session_maker() as session:
        assert (
            await review_deadline_binding(session, quarantine_id=quarantine_id) is None
        )
    async with session_maker() as session, session.begin():
        quarantine = await session.get(ScreeningQuarantine, quarantine_id)
        assert quarantine is not None
        quarantine.manifest_digest = "b2" * 32

    # A changed current artifact never authorizes a deadline on the old lease.
    async with session_maker() as session, session.begin():
        agent = await session.get(Agent, agent_id)
        assert agent is not None
        agent.sha256 = "d4" * 32
    async with session_maker() as session:
        assert (
            await review_deadline_binding(session, quarantine_id=quarantine_id) is None
        )


async def test_activation_rejects_backdating(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    with pytest.raises(IntegrityError):
        async with session_maker() as session, session.begin():
            session.add(
                ScreeningReviewDeadlineActivation(
                    policy_version=13,
                    policy_digest="e5" * 32,
                    activate_at=now - timedelta(minutes=1),
                    window_seconds=3600,
                    reason="forbidden backdated activation",
                    actor="test-operator",
                    created_at=now,
                )
            )
