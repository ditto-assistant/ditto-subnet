"""Tests for the cap that parks repeatedly-inconclusive agents for review.

Exercises the real ORM + SQLite-in-memory engine (same as the sibling query
tests) so the attempt/quarantine rows and the agent transition are real.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.screener import SCREENING_POLICY_VERSION
from ditto.db.models import (
    Agent,
    AgentStatus,
    AthReview,
    BenchmarkDataset,
    BenchmarkRollout,
    BenchmarkRolloutMember,
    ScreenerHeartbeat,
    ScreeningAttempt,
    ScreeningQuarantine,
)
from ditto.db.queries.screening import (
    _EXHAUSTED_REASON_CODE,
    MAX_SCREENING_EXPIRIES,
    claim_screening_attempts,
)

_SCREENER = "5GScreenerHotkeyForClaimTests000000000000000000000"
# The era that has activated, and the one a rollout would be collecting toward.
# Screening only cares that an agent holds a dataset for whichever era is
# authoritative, so these were the arbitrary v4 and v6. The floor pins them to
# real ones: nothing at or below v6 can be a rollout target any more.
_ACTIVE_VERSION = 7
_DESIRED_VERSION = 8


async def _seed_failed_agent(session: AsyncSession) -> Agent:
    agent = Agent(
        agent_id=uuid4(),
        miner_hotkey="5HKMinerHotkey",
        name="inconclusive-agent",
        sha256="de" * 32,
        status=AgentStatus.SCREENING_FAILED,
    )
    agent.screening_policy_version = SCREENING_POLICY_VERSION
    async with session.begin():
        session.add(agent)
    return agent


async def _seed_failed_agent_with_age(
    session: AsyncSession, *, name: str, age: timedelta
) -> Agent:
    agent = Agent(
        agent_id=uuid4(),
        miner_hotkey=f"5HK-{name}",
        name=name,
        sha256=uuid4().hex * 2,
        status=AgentStatus.SCREENING_FAILED,
        created_at=datetime.now(UTC) - age,
    )
    agent.screening_policy_version = SCREENING_POLICY_VERSION
    async with session.begin():
        session.add(agent)
    return agent


async def _add_expired_attempts(
    session: AsyncSession,
    agent: Agent,
    count: int,
    *,
    policy_version: int = SCREENING_POLICY_VERSION,
    base: datetime | None = None,
) -> None:
    base = base or datetime.now(UTC) - timedelta(hours=6)
    async with session.begin():
        for index in range(count):
            started = base + timedelta(minutes=45 * index)
            session.add(
                ScreeningAttempt(
                    attempt_id=uuid4(),
                    agent_id=agent.agent_id,
                    screener_hotkey=_SCREENER,
                    policy_version=policy_version,
                    status="expired",
                    started_at=started,
                    deadline=started + timedelta(minutes=45),
                    finished_at=started + timedelta(minutes=45),
                    public_reason="Screening lease expired",
                )
            )


async def _seed_owner_and_duplicate(
    session: AsyncSession,
    *,
    owner_status: AgentStatus,
) -> tuple[Agent, Agent]:
    """Seed an earlier owner and a later, different-miner copy of the SAME bytes.

    The copy is a fresh UPLOADED submission, so it is claimable and the claim
    runs the cross-miner duplicate precheck against the owner.
    """
    sha256 = uuid4().hex * 2
    created = datetime.now(UTC) - timedelta(days=2)
    owner = Agent(
        agent_id=uuid4(),
        miner_hotkey="5HK-owner",
        name="original",
        sha256=sha256,
        status=owner_status,
        created_at=created,
    )
    owner.screening_policy_version = SCREENING_POLICY_VERSION
    duplicate = Agent(
        agent_id=uuid4(),
        miner_hotkey="5HK-copycat",
        name="copy",
        sha256=sha256,
        status=AgentStatus.UPLOADED,
        created_at=created + timedelta(days=1),
    )
    async with session.begin():
        session.add(owner)
        session.add(duplicate)
    return owner, duplicate


# A real screener finding, as opposed to the platform-raised exhaustion
# sentinel. The two are NOT interchangeable: only a screener finding is "for
# cause", so every for-cause test must state which one it seeds.
_SCREENER_FINDING_REASON_CODE = "source-review"


async def _add_quarantine(
    session: AsyncSession,
    agent: Agent,
    *,
    status: str,
    resolution: str | None = None,
    reason_code: str = _SCREENER_FINDING_REASON_CODE,
) -> None:
    """Attach one quarantine row (plus the attempt its FK requires) to an agent."""
    at = datetime.now(UTC) - timedelta(days=1)
    async with session.begin():
        attempt = ScreeningAttempt(
            attempt_id=uuid4(),
            agent_id=agent.agent_id,
            screener_hotkey=_SCREENER,
            policy_version=SCREENING_POLICY_VERSION,
            status="quarantined",
            started_at=at,
            deadline=at,
            finished_at=at,
            public_reason="held for review",
            reason_code=reason_code,
        )
        session.add(attempt)
        await session.flush()
        session.add(
            ScreeningQuarantine(
                quarantine_id=uuid4(),
                agent_id=agent.agent_id,
                attempt_id=attempt.attempt_id,
                screener_hotkey=_SCREENER,
                policy_version=SCREENING_POLICY_VERSION,
                manifest_digest="a" * 64,
                finding_digest=None,
                reason_code=reason_code,
                evidence=None,
                finding=None,
                status=status,
                resolved_at=None if resolution is None else at,
                resolved_by=None if resolution is None else "operator",
                resolution=resolution,
                resolution_reason=None if resolution is None else "operator decision",
            )
        )


async def _add_operator_clear(
    session: AsyncSession,
    agent: Agent,
    *,
    resolution: str,
    resolved_at: datetime,
) -> None:
    """Record the successful operator decision that grants a fresh budget."""
    async with session.begin():
        attempt = ScreeningAttempt(
            attempt_id=uuid4(),
            agent_id=agent.agent_id,
            screener_hotkey=_SCREENER,
            policy_version=SCREENING_POLICY_VERSION,
            status="quarantined",
            started_at=resolved_at - timedelta(minutes=1),
            deadline=resolved_at - timedelta(minutes=1),
            finished_at=resolved_at - timedelta(minutes=1),
            public_reason="Screening was inconclusive repeatedly",
            reason_code="repeatedly-inconclusive",
        )
        session.add(attempt)
        await session.flush()
        session.add(
            ScreeningQuarantine(
                quarantine_id=uuid4(),
                agent_id=agent.agent_id,
                attempt_id=attempt.attempt_id,
                screener_hotkey=_SCREENER,
                policy_version=SCREENING_POLICY_VERSION,
                manifest_digest="d" * 64,
                finding_digest=None,
                reason_code="repeatedly-inconclusive",
                evidence=None,
                finding=None,
                status="resolved",
                created_at=resolved_at - timedelta(minutes=1),
                resolved_at=resolved_at,
                resolved_by="operator",
                resolution=resolution,
                resolution_reason="operator cleared the hold",
            )
        )


def _claimed_duplicate(
    claimed: list[tuple[Agent, ScreeningAttempt, object]], agent: Agent
) -> tuple[ScreeningAttempt, object]:
    """Return the (attempt, duplicate_of) the claim produced for ``agent``."""
    for claimed_agent, attempt, duplicate_of in claimed:
        if claimed_agent.agent_id == agent.agent_id:
            return attempt, duplicate_of
    raise AssertionError("agent was not claimed")


async def _claim(
    session: AsyncSession,
    *,
    limit: int = 10,
    deferred_review_mode: str = "off",
) -> list:
    async with session.begin():
        return await claim_screening_attempts(
            session,
            screener_hotkey=_SCREENER,
            now=datetime.now(UTC),
            ttl=timedelta(minutes=45),
            limit=limit,
            deferred_review_mode=deferred_review_mode,
        )


async def test_claim_releases_heartbeat_proven_orphan_without_expiry_penalty(
    session: AsyncSession,
) -> None:
    now = datetime.now(UTC)
    agent = Agent(
        agent_id=uuid4(),
        miner_hotkey="5HK-orphaned-build",
        name="orphaned-build",
        sha256=uuid4().hex * 2,
        status=AgentStatus.SCREENING,
    )
    orphan = ScreeningAttempt(
        attempt_id=uuid4(),
        agent_id=agent.agent_id,
        screener_hotkey=_SCREENER,
        policy_version=SCREENING_POLICY_VERSION,
        status="running",
        started_at=now - timedelta(minutes=10),
        deadline=now + timedelta(minutes=35),
        build_only=True,
    )
    async with session.begin():
        session.add_all(
            [
                agent,
                orphan,
                ScreenerHeartbeat(
                    screener_hotkey=_SCREENER,
                    instance_id="screener-a",
                    software_version="0.21.0",
                    protocol_version=4,
                    policy_version=SCREENING_POLICY_VERSION,
                    state="polling",
                    active_agent_id=None,
                    first_seen_at=now - timedelta(days=1),
                    reported_at=now - timedelta(seconds=5),
                    seen_at=now - timedelta(seconds=5),
                    signature="ab" * 64,
                ),
            ]
        )

    claimed = await _claim(session, limit=1, deferred_review_mode="enforce")

    assert len(claimed) == 1
    assert claimed[0][0].agent_id == agent.agent_id
    assert claimed[0][1].attempt_id != orphan.attempt_id
    assert orphan.status == "failed"
    assert orphan.reason_code == "worker-lease-orphaned"


async def test_claim_preserves_attempt_reported_active_by_a_fresh_worker(
    session: AsyncSession,
) -> None:
    now = datetime.now(UTC)
    agent = Agent(
        agent_id=uuid4(),
        miner_hotkey="5HK-active-build",
        name="active-build",
        sha256=uuid4().hex * 2,
        status=AgentStatus.SCREENING,
    )
    attempt = ScreeningAttempt(
        attempt_id=uuid4(),
        agent_id=agent.agent_id,
        screener_hotkey=_SCREENER,
        policy_version=SCREENING_POLICY_VERSION,
        status="running",
        started_at=now - timedelta(minutes=10),
        deadline=now + timedelta(minutes=35),
        build_only=True,
    )
    async with session.begin():
        session.add_all(
            [
                agent,
                attempt,
                ScreenerHeartbeat(
                    screener_hotkey=_SCREENER,
                    instance_id="screener-a",
                    software_version="0.21.0",
                    protocol_version=4,
                    policy_version=SCREENING_POLICY_VERSION,
                    state="screening",
                    active_agent_id=agent.agent_id,
                    first_seen_at=now - timedelta(days=1),
                    reported_at=now - timedelta(seconds=5),
                    seen_at=now - timedelta(seconds=5),
                    signature="ab" * 64,
                ),
            ]
        )

    claimed = await _claim(session, limit=1, deferred_review_mode="enforce")

    assert claimed == []
    assert attempt.status == "running"


@pytest.mark.parametrize("mode", ["off", "observe"])
async def test_only_enforce_uses_mechanical_first_claim(
    session: AsyncSession, mode: str
) -> None:
    agent = Agent(
        agent_id=uuid4(),
        miner_hotkey=f"5HK-{mode}",
        name=f"fresh-{mode}",
        sha256=uuid4().hex * 2,
        status=AgentStatus.UPLOADED,
    )
    async with session.begin():
        session.add(agent)

    claimed = await _claim(session, deferred_review_mode=mode)
    attempt, _duplicate = _claimed_duplicate(claimed, agent)

    assert attempt.build_only is False


async def test_enforce_uses_mechanical_first_then_one_deep_claim(
    session: AsyncSession,
) -> None:
    agent = Agent(
        agent_id=uuid4(),
        miner_hotkey="5HK-enforce",
        name="fresh-enforce",
        sha256=uuid4().hex * 2,
        status=AgentStatus.UPLOADED,
    )
    async with session.begin():
        session.add(agent)

    claimed = await _claim(session, deferred_review_mode="enforce")
    mechanical, _duplicate = _claimed_duplicate(claimed, agent)
    assert mechanical.build_only is True
    assert mechanical.reason_code == "deferred-mechanical-admission"

    opened_at = datetime.now(UTC)
    async with session.begin():
        mechanical.status = "passed"
        mechanical.finished_at = opened_at
        agent.status = AgentStatus.ATH_PENDING_REVIEW
        session.add(
            AthReview(
                review_id=uuid4(),
                agent_id=agent.agent_id,
                status="pending",
                opened_at=opened_at,
                original_reason="deferred review",
                original_policy_version=SCREENING_POLICY_VERSION,
                original_evidence={"previous_status": AgentStatus.SCORED.value},
                algorithm_provenance={
                    "review_kind": "deferred_source_review",
                },
            )
        )

    # Rollback is a true stop: off mode preserves the hold/evidence for manual
    # adjudication but starts no new expensive deep review.
    assert await _claim(session, deferred_review_mode="off") == []

    claimed = await _claim(session, deferred_review_mode="enforce")
    deep, _duplicate = _claimed_duplicate(claimed, agent)
    assert deep.build_only is False
    assert deep.reason_code is None

    async with session.begin():
        deep.status = "passed"
        deep.finished_at = datetime.now(UTC)

    # Any terminal deep result is the one allowed attempt. The pending ATH row
    # now waits for operator adjudication instead of looping back to a screener.
    assert await _claim(session, deferred_review_mode="enforce") == []


async def test_retryable_deep_infrastructure_failure_is_reclaimable(
    session: AsyncSession,
) -> None:
    agent = Agent(
        agent_id=uuid4(),
        miner_hotkey="5HK-deep-infra",
        name="deep-infra",
        sha256=uuid4().hex * 2,
        status=AgentStatus.ATH_PENDING_REVIEW,
    )
    opened_at = datetime.now(UTC) - timedelta(minutes=2)
    async with session.begin():
        session.add(agent)
        await session.flush()
        session.add(
            AthReview(
                review_id=uuid4(),
                agent_id=agent.agent_id,
                status="pending",
                opened_at=opened_at,
                original_reason="deferred review",
                original_policy_version=SCREENING_POLICY_VERSION,
                original_evidence={"previous_status": AgentStatus.SCORED.value},
                algorithm_provenance={
                    "review_kind": "deferred_source_review",
                },
            )
        )
        session.add(
            ScreeningAttempt(
                attempt_id=uuid4(),
                agent_id=agent.agent_id,
                screener_hotkey=_SCREENER,
                policy_version=SCREENING_POLICY_VERSION,
                status="failed",
                started_at=opened_at + timedelta(seconds=1),
                deadline=opened_at + timedelta(minutes=1),
                finished_at=opened_at + timedelta(minutes=1),
                build_only=False,
            )
        )

    claimed = await _claim(session, deferred_review_mode="enforce")
    retry, _duplicate = _claimed_duplicate(claimed, agent)
    assert retry.build_only is False


async def test_agent_parked_for_review_after_expiry_cap(session: AsyncSession):
    agent = await _seed_failed_agent(session)
    await _add_expired_attempts(session, agent, MAX_SCREENING_EXPIRIES)

    claimed = await _claim(session)

    # It must not be leased out again...
    claimed_ids = {claimed_agent.agent_id for claimed_agent, _, _ in claimed}
    assert agent.agent_id not in claimed_ids
    # ...it is quarantined for operator review...
    refreshed = await session.get(Agent, agent.agent_id)
    assert refreshed is not None
    assert refreshed.status == AgentStatus.QUARANTINED
    assert refreshed.screening_reason_code == "repeatedly-inconclusive"
    # ...with an active quarantine row (what the operator console lists).
    quarantine = await session.scalar(
        select(ScreeningQuarantine).where(
            ScreeningQuarantine.agent_id == agent.agent_id
        )
    )
    assert quarantine is not None
    assert quarantine.status == "active"
    assert quarantine.reason_code == "repeatedly-inconclusive"
    assert len(quarantine.manifest_digest) == 64


async def test_agent_still_claimed_below_the_cap(session: AsyncSession):
    agent = await _seed_failed_agent(session)
    await _add_expired_attempts(session, agent, MAX_SCREENING_EXPIRIES - 1)

    claimed = await _claim(session)

    claimed_ids = {claimed_agent.agent_id for claimed_agent, _, _ in claimed}
    assert agent.agent_id in claimed_ids
    refreshed = await session.get(Agent, agent.agent_id)
    assert refreshed is not None
    assert refreshed.status == AgentStatus.SCREENING
    quarantine = await session.scalar(
        select(ScreeningQuarantine).where(
            ScreeningQuarantine.agent_id == agent.agent_id
        )
    )
    assert quarantine is None


async def test_fresh_agent_runs_before_older_retry(session: AsyncSession):
    retry = await _seed_failed_agent_with_age(
        session, name="older-retry", age=timedelta(days=2)
    )
    fresh = await _seed_failed_agent_with_age(
        session, name="fresh-work", age=timedelta(days=1)
    )
    await _add_expired_attempts(session, retry, 1)

    claimed = await _claim(session, limit=1)

    assert [agent.agent_id for agent, _, _ in claimed] == [fresh.agent_id]


async def test_retry_runs_before_a_later_arriving_fresh_agent(
    session: AsyncSession,
):
    retry = await _seed_failed_agent_with_age(
        session, name="retry", age=timedelta(days=2)
    )
    await _seed_failed_agent_with_age(
        session, name="later-fresh", age=timedelta(hours=1)
    )
    await _add_expired_attempts(session, retry, 1)

    claimed = await _claim(session, limit=1)

    assert [agent.agent_id for agent, _, _ in claimed] == [retry.agent_id]


async def test_previous_policy_attempt_does_not_defer_current_policy_work(
    session: AsyncSession,
):
    older = await _seed_failed_agent_with_age(
        session, name="older-policy-history", age=timedelta(days=2)
    )
    await _seed_failed_agent_with_age(
        session, name="newer-current-work", age=timedelta(days=1)
    )
    await _add_expired_attempts(
        session,
        older,
        1,
        policy_version=SCREENING_POLICY_VERSION - 1,
        base=datetime.now(UTC) - timedelta(minutes=45),
    )

    claimed = await _claim(session, limit=1)

    assert [agent.agent_id for agent, _, _ in claimed] == [older.agent_id]


async def test_operator_rescreen_resets_the_expiry_budget(session: AsyncSession):
    """A quarantine resolved with ``rescreen`` grants a fresh attempt budget.

    Regression for 2026-07-16: agents whose expiries came from a screener
    fleet outage were instantly re-parked on the next claim after an operator
    rescreen, because the expiry count ignored the rescreen entirely.
    """
    agent = await _seed_failed_agent(session)
    base = datetime.now(UTC) - timedelta(hours=6)
    await _add_expired_attempts(session, agent, MAX_SCREENING_EXPIRIES, base=base)
    # The exhaustion park + the operator's rescreen, AFTER the expiries.
    async with session.begin():
        park_attempt = ScreeningAttempt(
            attempt_id=uuid4(),
            agent_id=agent.agent_id,
            screener_hotkey=_SCREENER,
            policy_version=SCREENING_POLICY_VERSION,
            status="quarantined",
            started_at=base + timedelta(hours=5),
            deadline=base + timedelta(hours=5),
            finished_at=base + timedelta(hours=5),
            public_reason="Screening was inconclusive repeatedly",
            reason_code="repeatedly-inconclusive",
        )
        session.add(park_attempt)
        await session.flush()
        session.add(
            ScreeningQuarantine(
                quarantine_id=uuid4(),
                agent_id=agent.agent_id,
                attempt_id=park_attempt.attempt_id,
                screener_hotkey=_SCREENER,
                policy_version=SCREENING_POLICY_VERSION,
                manifest_digest="d" * 64,
                finding_digest=None,
                reason_code="repeatedly-inconclusive",
                evidence=None,
                finding=None,
                status="resolved",
                resolved_at=datetime.now(UTC) - timedelta(minutes=30),
                resolved_by="operator",
                resolution="rescreen",
                resolution_reason="fleet outage, not agent behavior",
            )
        )

    claimed = await _claim(session)

    # The rescreen zeroed the budget: the agent is leased out for a REAL run,
    # not instantly re-parked.
    claimed_ids = {claimed_agent.agent_id for claimed_agent, _, _ in claimed}
    assert agent.agent_id in claimed_ids
    refreshed = await session.get(Agent, agent.agent_id)
    assert refreshed is not None
    assert refreshed.status == AgentStatus.SCREENING


async def test_expiries_after_a_rescreen_still_exhaust(session: AsyncSession):
    """Only pre-rescreen expiries are forgiven; the cap still protects the pool."""
    agent = await _seed_failed_agent(session)
    rescreened_at = datetime.now(UTC) - timedelta(hours=5)
    async with session.begin():
        anchor = ScreeningAttempt(
            attempt_id=uuid4(),
            agent_id=agent.agent_id,
            screener_hotkey=_SCREENER,
            policy_version=SCREENING_POLICY_VERSION,
            status="quarantined",
            started_at=rescreened_at - timedelta(minutes=1),
            deadline=rescreened_at - timedelta(minutes=1),
            finished_at=rescreened_at - timedelta(minutes=1),
            public_reason="parked",
            reason_code="repeatedly-inconclusive",
        )
        session.add(anchor)
        await session.flush()
        session.add(
            ScreeningQuarantine(
                quarantine_id=uuid4(),
                agent_id=agent.agent_id,
                attempt_id=anchor.attempt_id,
                screener_hotkey=_SCREENER,
                policy_version=SCREENING_POLICY_VERSION,
                manifest_digest="d" * 64,
                finding_digest=None,
                reason_code="repeatedly-inconclusive",
                evidence=None,
                finding=None,
                status="resolved",
                resolved_at=rescreened_at,
                resolved_by="operator",
                resolution="rescreen",
                resolution_reason="grant a fresh budget",
            )
        )
    # A fresh cap's worth of expiries AFTER the rescreen…
    await _add_expired_attempts(
        session,
        agent,
        MAX_SCREENING_EXPIRIES,
        base=rescreened_at + timedelta(minutes=5),
    )

    claimed = await _claim(session)

    # …parks it again: the reset is not a permanent exemption.
    claimed_ids = {claimed_agent.agent_id for claimed_agent, _, _ in claimed}
    assert agent.agent_id not in claimed_ids
    refreshed = await session.get(Agent, agent.agent_id)
    assert refreshed is not None
    assert refreshed.status == AgentStatus.QUARANTINED


async def test_duplicate_flagged_when_owner_rejected_with_reject_resolution(
    session: AsyncSession,
):
    """A copy of an artifact whose original was rejected FOR CAUSE is flagged.

    The 716ditto case: refusing the original used to remove it from the owner
    set, so the very act of adjudicating the first copy disarmed the detector
    for every later identical submission.
    """
    owner, duplicate = await _seed_owner_and_duplicate(
        session, owner_status=AgentStatus.REJECTED
    )
    await _add_quarantine(session, owner, status="resolved", resolution="reject")

    claimed = await _claim(session)

    attempt, duplicate_of = _claimed_duplicate(claimed, duplicate)
    assert duplicate_of == owner.agent_id
    assert attempt.reason_code == "exact-cross-miner-duplicate"
    assert attempt.duplicate_of == owner.agent_id


async def test_duplicate_flagged_when_owner_banned_without_quarantine(
    session: AsyncSession,
):
    """BANNED is for-cause on its own; a ban may be issued with no quarantine row."""
    owner, duplicate = await _seed_owner_and_duplicate(
        session, owner_status=AgentStatus.BANNED
    )

    claimed = await _claim(session)

    attempt, duplicate_of = _claimed_duplicate(claimed, duplicate)
    assert duplicate_of == owner.agent_id
    assert attempt.reason_code == "exact-cross-miner-duplicate"


async def test_duplicate_flagged_when_owner_has_active_quarantine(
    session: AsyncSession,
):
    """An outstanding SCREENER FINDING counts as for-cause while the operator decides.

    The finding reason_code is asserted explicitly: an active quarantine alone
    is not enough, since the platform raises active quarantines of its own for
    exhausted attempts (see the exhaustion-sentinel test below). If this test
    silently seeded the sentinel it would be passing for the wrong reason.
    """
    owner, duplicate = await _seed_owner_and_duplicate(
        session, owner_status=AgentStatus.QUARANTINED
    )
    await _add_quarantine(
        session, owner, status="active", reason_code=_SCREENER_FINDING_REASON_CODE
    )
    assert _SCREENER_FINDING_REASON_CODE != _EXHAUSTED_REASON_CODE

    claimed = await _claim(session)

    attempt, duplicate_of = _claimed_duplicate(claimed, duplicate)
    assert duplicate_of == owner.agent_id
    assert attempt.reason_code == "exact-cross-miner-duplicate"


async def test_duplicate_not_flagged_when_owner_parked_as_inconclusive(
    session: AsyncSession,
):
    """False-positive guard: a platform-raised exhaustion park is not for cause.

    ``_park_repeatedly_inconclusive`` writes an ACTIVE quarantine carrying the
    ``repeatedly-inconclusive`` sentinel when an agent keeps expiring its lease.
    That is an infrastructure outcome, not a provenance finding — the 2026-07-16
    incident parked 12 agents purely from a screener-fleet outage. Treating such
    a park as for-cause would let an outage condemn every later identical
    cross-miner submission, which is the same false positive the build/infra
    rejection guard above exists to prevent.
    """
    owner, duplicate = await _seed_owner_and_duplicate(
        session, owner_status=AgentStatus.QUARANTINED
    )
    await _add_quarantine(
        session, owner, status="active", reason_code=_EXHAUSTED_REASON_CODE
    )

    claimed = await _claim(session)

    attempt, duplicate_of = _claimed_duplicate(claimed, duplicate)
    assert duplicate_of is None
    assert attempt.reason_code is None


async def test_duplicate_flagged_when_operator_rejects_an_inconclusive_park(
    session: AsyncSession,
):
    """Human judgement overrides the infra origin of the park.

    The sentinel only excuses the park itself. Once an operator reviewed it and
    resolved ``reject``, that IS an adjudicated refusal for cause, so the owner
    is a valid duplicate owner again regardless of how the hold started.
    """
    owner, duplicate = await _seed_owner_and_duplicate(
        session, owner_status=AgentStatus.REJECTED
    )
    await _add_quarantine(
        session,
        owner,
        status="resolved",
        resolution="reject",
        reason_code=_EXHAUSTED_REASON_CODE,
    )

    claimed = await _claim(session)

    attempt, duplicate_of = _claimed_duplicate(claimed, duplicate)
    assert duplicate_of == owner.agent_id
    assert attempt.reason_code == "exact-cross-miner-duplicate"


async def test_duplicate_not_flagged_when_owner_rejected_without_quarantine(
    session: AsyncSession,
):
    """False-positive guard: a build/infra rejection must not condemn a copy.

    Such a rejection writes no quarantine row, so nothing was ever adjudicated
    about the artifact's provenance. Flagging here would punish an honest
    resubmission for the platform's own build failure.
    """
    owner, duplicate = await _seed_owner_and_duplicate(
        session, owner_status=AgentStatus.REJECTED
    )

    claimed = await _claim(session)

    attempt, duplicate_of = _claimed_duplicate(claimed, duplicate)
    assert duplicate_of is None
    assert attempt.reason_code is None


async def test_duplicate_not_flagged_when_owner_quarantine_released(
    session: AsyncSession,
):
    """An operator ``release`` deliberately clears the finding.

    The agent row's screening_reason_code is wiped by a re-screen, so the
    for-cause test reads quarantine history; a released hold must read as
    "cleared", not as a standing finding that condemns later copies.
    """
    owner, duplicate = await _seed_owner_and_duplicate(
        session, owner_status=AgentStatus.REJECTED
    )
    await _add_quarantine(session, owner, status="resolved", resolution="release")

    claimed = await _claim(session)

    attempt, duplicate_of = _claimed_duplicate(claimed, duplicate)
    assert duplicate_of is None
    assert attempt.reason_code is None


async def test_duplicate_not_flagged_when_owner_quarantine_rescreened(
    session: AsyncSession,
):
    """``rescreen`` is likewise an operator clearing the hold, not a finding."""
    owner, duplicate = await _seed_owner_and_duplicate(
        session, owner_status=AgentStatus.REJECTED
    )
    await _add_quarantine(session, owner, status="resolved", resolution="rescreen")

    claimed = await _claim(session)

    attempt, duplicate_of = _claimed_duplicate(claimed, duplicate)
    assert duplicate_of is None
    assert attempt.reason_code is None


async def test_duplicate_flagged_when_owner_is_usable(session: AsyncSession):
    """Unchanged behavior: live work being copied is still flagged."""
    owner, duplicate = await _seed_owner_and_duplicate(
        session, owner_status=AgentStatus.EVALUATING
    )

    claimed = await _claim(session)

    attempt, duplicate_of = _claimed_duplicate(claimed, duplicate)
    assert duplicate_of == owner.agent_id
    assert attempt.reason_code == "exact-cross-miner-duplicate"


async def _seed_agent_at_policy(
    session: AsyncSession, *, status: AgentStatus, policy_version: int
) -> Agent:
    agent = Agent(
        agent_id=uuid4(),
        miner_hotkey="5HK-requeue",
        name="stale-policy-agent",
        sha256=uuid4().hex * 2,
        status=status,
    )
    agent.screening_policy_version = policy_version
    async with session.begin():
        session.add(agent)
    return agent


async def test_rejected_agent_is_not_auto_requeued_on_policy_bump(
    session: AsyncSession,
):
    """A refused artifact must not return just because the policy version moved.

    Auto-requeue resurrected every past rejection fleet-wide and cleared the
    operator's stated reason, letting a refused artifact back in under a policy
    that never re-derived the original finding.
    """
    agent = await _seed_agent_at_policy(
        session,
        status=AgentStatus.REJECTED,
        policy_version=SCREENING_POLICY_VERSION - 1,
    )

    claimed = await _claim(session)

    assert agent.agent_id not in {
        claimed_agent.agent_id for claimed_agent, _, _ in claimed
    }
    refreshed = await session.get(Agent, agent.agent_id)
    assert refreshed is not None
    assert refreshed.status == AgentStatus.REJECTED


async def test_appealed_agent_in_screening_failed_is_claimable(session: AsyncSession):
    """The operator appeal endpoint moves REJECTED -> SCREENING_FAILED.

    That is the ONLY re-entry path now, so it must still be claimable.
    """
    agent = await _seed_agent_at_policy(
        session,
        status=AgentStatus.SCREENING_FAILED,
        policy_version=SCREENING_POLICY_VERSION - 1,
    )

    claimed = await _claim(session)

    assert agent.agent_id in {claimed_agent.agent_id for claimed_agent, _, _ in claimed}
    refreshed = await session.get(Agent, agent.agent_id)
    assert refreshed is not None
    assert refreshed.status == AgentStatus.SCREENING


async def _activate_current_era(session: AsyncSession) -> None:
    """The durable authority: the era screening asks agents to hold a dataset for.

    Nothing below is about a particular benchmark -- these tests ask whether a
    stuck EVALUATING agent is re-claimed -- so the transition used to be the
    arbitrary v2 -> v4. ``benchmark_rollout_desired_floor`` refuses a rollout
    aimed under MIN_SCOREABLE_BENCH_VERSION now, so it is the real v6 -> v7 one.
    """
    now = datetime.now(UTC)
    async with session.begin():
        session.add(
            BenchmarkRollout(
                rollout_id=uuid4(),
                from_version=_ACTIVE_VERSION - 1,
                desired_version=_ACTIVE_VERSION,
                status="activated",
                cohort_size=5,
                created_at=now - timedelta(hours=1),
                activated_at=now,
            )
        )


def _complete_screened_image(agent: Agent) -> None:
    agent.screened_image_sha256 = "ab" * 32
    agent.screened_image_size_bytes = 4096
    agent.screened_image_id = "sha256:" + "cd" * 32
    agent.screened_image_ref = "registry/agent:screened"
    agent.screened_image_upload_id = uuid4()
    agent.screened_image_verified_at = datetime.now(UTC)


async def test_historical_unadmitted_agent_is_not_reclaimed_for_prerequisites(
    session: AsyncSession,
) -> None:
    """A rebuild that cannot lead to a validator lease must not consume a screen."""
    now = datetime.now(UTC)
    agent = Agent(
        agent_id=uuid4(),
        miner_hotkey="5HK-historical-unadmitted",
        name="historical-unadmitted",
        sha256=uuid4().hex * 2,
        status=AgentStatus.EVALUATING,
        created_at=now - timedelta(hours=2),
    )
    agent.screening_policy_version = SCREENING_POLICY_VERSION
    _complete_screened_image(agent)
    async with session.begin():
        session.add(agent)
        session.add(
            BenchmarkRollout(
                rollout_id=uuid4(),
                from_version=_ACTIVE_VERSION - 1,
                desired_version=_ACTIVE_VERSION,
                status="activated",
                cohort_size=5,
                created_at=now - timedelta(hours=1),
                activated_at=now,
            )
        )

    claimed = await _claim(session)

    assert agent.agent_id not in {
        claimed_agent.agent_id for claimed_agent, _, _ in claimed
    }
    refreshed = await session.get(Agent, agent.agent_id)
    assert refreshed is not None and refreshed.status == AgentStatus.EVALUATING


async def test_historical_rollout_member_is_reclaimed_for_prerequisites(
    session: AsyncSession,
) -> None:
    """Frozen rollout admission still authorizes a missing-dataset rebuild."""
    now = datetime.now(UTC)
    rollout_id = uuid4()
    agent = Agent(
        agent_id=uuid4(),
        miner_hotkey="5HK-historical-member",
        name="historical-member",
        sha256=uuid4().hex * 2,
        status=AgentStatus.EVALUATING,
        created_at=now - timedelta(hours=2),
    )
    agent.screening_policy_version = SCREENING_POLICY_VERSION
    _complete_screened_image(agent)
    async with session.begin():
        session.add(agent)
        session.add(
            BenchmarkRollout(
                rollout_id=rollout_id,
                from_version=_ACTIVE_VERSION - 1,
                desired_version=_ACTIVE_VERSION,
                status="activated",
                cohort_size=10,
                created_at=now - timedelta(hours=1),
                activated_at=now,
            )
        )
        session.add(
            BenchmarkRolloutMember(
                rollout_id=rollout_id,
                agent_id=agent.agent_id,
                position=1,
                frozen_miner_hotkey=agent.miner_hotkey,
                frozen_composite=0.9,
            )
        )

    claimed = await _claim(session)

    attempt = next(
        (
            attempt
            for claimed_agent, attempt, _ in claimed
            if claimed_agent.agent_id == agent.agent_id
        ),
        None,
    )
    assert attempt is not None and attempt.build_only is True


async def test_evaluating_agent_missing_screened_image_is_reclaimed(
    session: AsyncSession,
) -> None:
    # v7 (which requires a screened image) is active. An agent released from an
    # anti-cheat quarantine back to EVALUATING on the current policy, but whose
    # screened image was never uploaded+verified, is otherwise stuck forever:
    # validators skip it and nothing re-screens it. It must be re-claimed.
    await _activate_current_era(session)
    agent = Agent(
        agent_id=uuid4(),
        miner_hotkey="5HK-no-image",
        name="released-without-image",
        sha256=uuid4().hex * 2,
        status=AgentStatus.EVALUATING,
    )
    agent.screening_policy_version = SCREENING_POLICY_VERSION
    async with session.begin():
        session.add(agent)

    claimed = await _claim(session)

    attempt = next(
        (
            a
            for claimed_agent, a, _ in claimed
            if claimed_agent.agent_id == agent.agent_id
        ),
        None,
    )
    assert attempt is not None
    refreshed = await session.get(Agent, agent.agent_id)
    assert refreshed is not None and refreshed.status == AgentStatus.SCREENING
    # It already cleared the anti-cheat review (it was EVALUATING on the current
    # policy), so this is a BUILD-ONLY pass — rebuild the image, do not re-review.
    assert attempt.build_only is True


async def test_release_after_expiry_cap_gets_build_only_attempt(
    session: AsyncSession,
) -> None:
    """A release supersedes old failures before prerequisite-only recovery.

    Regression for affu-03: five historical HTTP failures survived an operator
    release, so the platform re-parked the agent before it could issue the
    build-only attempt needed to create its missing screened image.
    """
    await _activate_current_era(session)
    agent = Agent(
        agent_id=uuid4(),
        miner_hotkey="5HK-release-after-expiries",
        name="released-after-expiries",
        sha256=uuid4().hex * 2,
        status=AgentStatus.EVALUATING,
    )
    agent.screening_policy_version = SCREENING_POLICY_VERSION
    async with session.begin():
        session.add(agent)

    base = datetime.now(UTC) - timedelta(hours=6)
    await _add_expired_attempts(session, agent, MAX_SCREENING_EXPIRIES, base=base)
    await _add_operator_clear(
        session,
        agent,
        resolution="release",
        resolved_at=datetime.now(UTC) - timedelta(minutes=30),
    )

    claimed = await _claim(session)

    attempt, _ = _claimed_duplicate(claimed, agent)
    assert attempt.build_only is True
    refreshed = await session.get(Agent, agent.agent_id)
    assert refreshed is not None and refreshed.status == AgentStatus.SCREENING
    active_quarantine = await session.scalar(
        select(ScreeningQuarantine).where(
            ScreeningQuarantine.agent_id == agent.agent_id,
            ScreeningQuarantine.status == "active",
        )
    )
    assert active_quarantine is None


async def test_expiries_after_release_still_exhaust(session: AsyncSession) -> None:
    """Release resets historical failures, but it is not a permanent bypass."""
    await _activate_current_era(session)
    agent = Agent(
        agent_id=uuid4(),
        miner_hotkey="5HK-new-expiries-after-release",
        name="new-expiries-after-release",
        sha256=uuid4().hex * 2,
        status=AgentStatus.EVALUATING,
    )
    agent.screening_policy_version = SCREENING_POLICY_VERSION
    async with session.begin():
        session.add(agent)

    released_at = datetime.now(UTC) - timedelta(hours=5)
    await _add_operator_clear(
        session,
        agent,
        resolution="release",
        resolved_at=released_at,
    )
    await _add_expired_attempts(
        session,
        agent,
        MAX_SCREENING_EXPIRIES,
        base=released_at + timedelta(minutes=5),
    )

    claimed = await _claim(session)

    assert agent.agent_id not in {a.agent_id for a, _, _ in claimed}
    refreshed = await session.get(Agent, agent.agent_id)
    assert refreshed is not None
    assert refreshed.status == AgentStatus.QUARANTINED
    assert refreshed.screening_reason_code == "repeatedly-inconclusive"


async def test_fresh_upload_claim_is_not_build_only(
    session: AsyncSession,
) -> None:
    # A never-reviewed submission gets the full screen (review can quarantine),
    # not a build-only pass.
    agent = await _seed_failed_agent(session)  # SCREENING_FAILED, never passed
    claimed = await _claim(session)
    attempt = next(
        (
            a
            for claimed_agent, a, _ in claimed
            if claimed_agent.agent_id == agent.agent_id
        ),
        None,
    )
    assert attempt is not None
    assert attempt.build_only is False


async def test_evaluating_agent_with_complete_prereqs_is_not_reclaimed(
    session: AsyncSession,
) -> None:
    # The mirror: a current-policy EVALUATING agent that HAS a complete screened
    # image and the active-version dataset needs no re-screen — it must not be
    # dragged back through screening by either predicate.
    await _activate_current_era(session)
    agent = Agent(
        agent_id=uuid4(),
        miner_hotkey="5HK-complete",
        name="fully-provisioned",
        sha256=uuid4().hex * 2,
        status=AgentStatus.EVALUATING,
    )
    agent.screening_policy_version = SCREENING_POLICY_VERSION
    _complete_screened_image(agent)
    async with session.begin():
        session.add(agent)
        session.add(
            BenchmarkDataset(
                agent_id=agent.agent_id,
                bench_version=_ACTIVE_VERSION,
                seed=7,
                sha256="ef" * 32,
                run_size="full",
                seed_block=100,
                seed_block_hash="ff" * 32,
            )
        )

    claimed = await _claim(session)

    assert agent.agent_id not in {a.agent_id for a, _, _ in claimed}


async def test_effective_rollout_authority_dataset_is_not_reclaimed(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The rollout start guard can make the DESIRED version the effective
    # authority while the durable activated row still names the active one.
    # Backroom and ticketing correctly report v8 in that state; screening must
    # use the same authority instead of asking this already-complete agent for
    # an obsolete v7 dataset.
    await _activate_current_era(session)
    now = datetime.now(UTC)
    agent = Agent(
        agent_id=uuid4(),
        miner_hotkey="5HK-effective-desired",
        name="effective-desired-complete",
        sha256=uuid4().hex * 2,
        status=AgentStatus.EVALUATING,
        created_at=now - timedelta(days=1),
    )
    agent.screening_policy_version = SCREENING_POLICY_VERSION
    _complete_screened_image(agent)
    async with session.begin():
        session.add(
            BenchmarkRollout(
                rollout_id=uuid4(),
                from_version=_ACTIVE_VERSION,
                desired_version=_DESIRED_VERSION,
                status="collecting",
                cohort_size=5,
                created_at=now - timedelta(minutes=5),
            )
        )
        session.add(agent)
        session.add(
            BenchmarkDataset(
                agent_id=agent.agent_id,
                bench_version=_DESIRED_VERSION,
                seed=7,
                sha256="ef" * 32,
                run_size="full",
                seed_block=100,
                seed_block_hash="ff" * 32,
            )
        )

    async def effective_desired(_session: AsyncSession) -> int:
        return _DESIRED_VERSION

    monkeypatch.setattr(
        "ditto.db.queries.screening.active_bench_version", effective_desired
    )

    claimed = await _claim(session)

    assert agent.agent_id not in {a.agent_id for a, _, _ in claimed}
