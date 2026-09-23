"""A policy recommendation alone cannot create a live v13 deadline."""

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.screener_review_settings import (
    ScreenerReviewSettings,
    policy_manifest_digest,
    review_settings_checksum,
)
from ditto.db.models import (
    Agent,
    ScreenerReviewSettingsRevision,
    ScreeningAttempt,
    ScreeningQuarantine,
    ScreeningReviewDeadlineActivation,
    ScreeningReviewWindow,
)
from ditto.db.queries.screening_review_deadlines import (
    POLICY_V13_DOCUMENT_DIGEST,
    record_first_v13_claim_window,
    review_deadline_binding,
)

pytestmark = pytest.mark.asyncio


async def test_v13_document_digest_matches_published_policy() -> None:
    policy = next(
        root / "workers/screener/docs/policy-v13.md"
        for root in Path(__file__).resolve().parents
        if (root / "workers/screener/docs/policy-v13.md").is_file()
    )
    assert hashlib.sha256(policy.read_bytes()).hexdigest() == POLICY_V13_DOCUMENT_DIGEST


async def test_first_claim_clock_is_exact_and_not_restarted(
    session_maker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    from ditto.db.queries import screening_review_deadlines as clock_queries

    now = datetime.now(UTC)
    activation_time = now + timedelta(hours=1)
    claim_time = activation_time + timedelta(minutes=1)

    async def fixed_db_clock(_session: AsyncSession) -> datetime:
        return claim_time

    monkeypatch.setattr(clock_queries, "_database_clock", fixed_db_clock)
    settings = ScreenerReviewSettings()
    manifest = policy_manifest_digest(
        settings.policy_manifest_profile, settings.policy_manifest_rotation_id
    )
    agent_id, first_id, retry_id = uuid4(), uuid4(), uuid4()
    artifact_sha = "a5" * 32
    async with session_maker() as session, session.begin():
        posture = ScreenerReviewSettingsRevision(
            parent_revision=0,
            scope="*",
            settings=settings.model_dump(mode="json"),
            checksum=review_settings_checksum(settings),
            reason="pinned clock posture",
            actor="operator",
        )
        session.add(posture)
        session.add(
            ScreeningReviewDeadlineActivation(
                policy_version=13,
                policy_digest=manifest,
                policy_document_digest=POLICY_V13_DOCUMENT_DIGEST,
                activate_at=activation_time,
                window_seconds=7200,
                reason="published two-hour window",
                actor="operator",
            )
        )
        await session.flush()
        posture_revision = posture.revision
        posture_checksum = posture.checksum
        session.add(
            Agent(
                agent_id=agent_id,
                miner_hotkey="5ClockTestMiner",
                name="clock-first-claim",
                sha256=artifact_sha,
                status=AgentStatus.UPLOADED,
            )
        )
        await session.flush()
        first = ScreeningAttempt(
            attempt_id=first_id,
            agent_id=agent_id,
            artifact_sha256=artifact_sha,
            screener_hotkey="5ClockTestScreener",
            policy_version=13,
            status="running",
            started_at=now,
            deadline=now + timedelta(minutes=5),
            build_only=True,
            review_settings_revision=posture_revision,
            review_settings_instance_id="clock-worker-1",
            review_settings_scope="*",
            review_settings_checksum=posture_checksum,
        )
        session.add(first)
        agent = await session.get(Agent, agent_id)
        assert agent is not None
        assert await record_first_v13_claim_window(
            session,
            agent=agent,
            attempt=first,
            lease_ttl=timedelta(minutes=5),
        )
        assert first.started_at == claim_time
        assert first.deadline == claim_time + timedelta(minutes=5)
    async with session_maker() as session, session.begin():
        loaded_first = await session.get(ScreeningAttempt, first_id)
        assert loaded_first is not None
        loaded_first.status = "expired"
        loaded_first.finished_at = claim_time + timedelta(minutes=5)
        await session.flush()
        window = await session.scalar(
            select(ScreeningReviewWindow).where(
                ScreeningReviewWindow.agent_id == agent_id
            )
        )
        assert window is not None
        assert window.start_event == "first-v13-screening-claim"
        assert window.first_attempt_id == first_id
        assert window.deadline_at == claim_time + timedelta(hours=2)
        retry = ScreeningAttempt(
            attempt_id=retry_id,
            agent_id=agent_id,
            artifact_sha256=artifact_sha,
            screener_hotkey="5ClockTestOtherWorker",
            policy_version=13,
            status="running",
            started_at=claim_time + timedelta(minutes=10),
            deadline=claim_time + timedelta(minutes=15),
            review_settings_revision=posture_revision,
            review_settings_instance_id="clock-worker-2",
            review_settings_scope="*",
            review_settings_checksum=posture_checksum,
        )
        session.add(retry)
        agent = await session.get(Agent, agent_id)
        assert agent is not None
        assert not await record_first_v13_claim_window(
            session, agent=agent, attempt=retry, lease_ttl=timedelta(minutes=5)
        )
        # Scheduling the clock after an earlier build-only claim must not
        # retroactively start a window on a subsequent claim for that UUID.
        legacy_agent_id = uuid4()
        legacy_sha = "c7" * 32
        session.add(
            Agent(
                agent_id=legacy_agent_id,
                miner_hotkey="5ClockLegacyMiner",
                name="clock-existing-hold",
                sha256=legacy_sha,
                status=AgentStatus.UPLOADED,
            )
        )
        session.add(
            ScreeningAttempt(
                attempt_id=uuid4(),
                agent_id=legacy_agent_id,
                artifact_sha256=legacy_sha,
                screener_hotkey="5ClockLegacyWorker",
                policy_version=13,
                status="expired",
                started_at=activation_time - timedelta(minutes=1),
                deadline=activation_time,
                build_only=True,
            )
        )
        await session.flush()
        legacy_retry = ScreeningAttempt(
            attempt_id=uuid4(),
            agent_id=legacy_agent_id,
            artifact_sha256=legacy_sha,
            screener_hotkey="5ClockNewWorker",
            policy_version=13,
            status="running",
            started_at=claim_time,
            deadline=claim_time + timedelta(minutes=5),
            review_settings_revision=posture_revision,
            review_settings_instance_id="clock-worker-3",
            review_settings_scope="*",
            review_settings_checksum=posture_checksum,
        )
        session.add(legacy_retry)
        legacy_agent = await session.get(Agent, legacy_agent_id)
        assert legacy_agent is not None
        assert not await record_first_v13_claim_window(
            session,
            agent=legacy_agent,
            attempt=legacy_retry,
            lease_ttl=timedelta(minutes=5),
        )
    async with session_maker() as session:
        windows = list(
            await session.scalars(
                select(ScreeningReviewWindow).where(
                    ScreeningReviewWindow.agent_id == agent_id
                )
            )
        )
        assert len(windows) == 1
        assert windows[0].first_attempt_id == first_id
        assert windows[0].agent_id != legacy_agent_id


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
            policy_document_digest="c3" * 32,
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
                start_event="first-v13-screening-claim",
                started_at=now + timedelta(hours=2),
                deadline_at=now + timedelta(hours=3),
            )
        )

    async with session_maker() as session:
        binding = await review_deadline_binding(session, quarantine_id=quarantine_id)
        assert binding is not None
        assert binding.artifact_sha256 == artifact_sha
        assert binding.manifest_digest == "b2" * 32
        assert binding.policy_document_digest == "c3" * 32
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
