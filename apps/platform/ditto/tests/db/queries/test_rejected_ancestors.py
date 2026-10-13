"""Rejection attention uses exact paid identities and still-rejected artifacts."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.db.models import (
    Agent,
    AgentStatus,
    AthReview,
    AthReviewAction,
    EvaluationPayment,
    ScreeningAttempt,
    ScreeningQuarantine,
)
from ditto.db.queries.rejected_ancestors import rejected_ancestors
from ditto_screening_protocol import SourceReviewFinding

_TIME = datetime(2026, 10, 1, tzinfo=UTC)


async def seed(
    session: AsyncSession,
    *,
    hotkey: str,
    coldkey: str | None = None,
    time: datetime = _TIME,
    status: AgentStatus = AgentStatus.REJECTED,
    bound: bool = True,
) -> Agent:
    agent = Agent(
        agent_id=uuid4(),
        miner_hotkey=hotkey,
        name="ancestor",
        sha256="a" * 64,
        status=status,
        created_at=time,
        screening_policy_version=13,
    )
    session.add(agent)
    await session.flush()
    if coldkey:
        session.add(
            EvaluationPayment(
                block_hash=str(agent.agent_id),
                extrinsic_index=0,
                agent_id=agent.agent_id,
                miner_hotkey=hotkey,
                miner_coldkey=coldkey,
                amount_rao=1,
                dest_address="dest",
                timestamp=time,
            )
        )
    review = AthReview(
        review_id=uuid4(),
        agent_id=agent.agent_id,
        status="resolved",
        resolution="reject",
        resolved_at=time,
        resolved_by="reviewer",
        resolution_reason="proved mechanism",
        original_policy_version=13,
        original_evidence={"sha256": agent.sha256 if bound else "b" * 64},
        algorithm_provenance={},
    )
    session.add(review)
    await session.flush()
    session.add(
        AthReviewAction(
            action_id=uuid4(),
            review_id=review.review_id,
            action="reject",
            reason="proved mechanism",
            actor="reviewer",
            created_at=time,
            evidence={
                "batch_ruling": {
                    "action": "reject",
                    "evidence_references": ["src/main.rs:3"],
                }
            },
        )
    )
    await session.flush()
    return agent


async def test_direct_owner_link_binding_and_chronology(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    async with session_maker() as session, session.begin():
        same_hotkey = await seed(session, hotkey="candidate")
        same_coldkey = await seed(session, hotkey="other", coldkey="payer")
        await seed(session, hotkey="unrelated", coldkey="someone-else")
        await seed(session, hotkey="candidate", bound=False)
        await seed(session, hotkey="candidate", status=AgentStatus.SCORED)
        await seed(session, hotkey="candidate", time=_TIME + timedelta(days=2))
        candidate = await seed(
            session,
            hotkey="candidate",
            coldkey="payer",
            time=_TIME + timedelta(days=1),
            status=AgentStatus.UPLOADED,
        )
        rows = await rejected_ancestors(session, candidate=candidate)
        assert {row.agent_id for row in rows} == {
            same_hotkey.agent_id,
            same_coldkey.agent_id,
        }
        assert all(
            row.sha256 == "a" * 64 and row.references == ("src/main.rs:3",)
            for row in rows
        )


async def test_ancestor_lookup_is_bounded_to_three_latest_artifacts(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    async with session_maker() as session, session.begin():
        ancestors = [
            await seed(session, hotkey="candidate", time=_TIME + timedelta(hours=n))
            for n in range(5)
        ]
        candidate = await seed(
            session,
            hotkey="candidate",
            time=_TIME + timedelta(days=1),
            status=AgentStatus.UPLOADED,
        )
        rows = await rejected_ancestors(session, candidate=candidate)
        assert [row.agent_id for row in rows] == [
            agent.agent_id for agent in reversed(ancestors[-3:])
        ]


async def test_screening_rejects_require_matching_finding_digest_and_artifact(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    async with session_maker() as session, session.begin():
        valid = None
        for tampered in (False, True):
            agent = Agent(
                agent_id=uuid4(),
                miner_hotkey="candidate",
                name="ancestor",
                sha256="a" * 64,
                status=AgentStatus.REJECTED,
                created_at=_TIME,
                screening_policy_version=13,
            )
            session.add(agent)
            await session.flush()
            attempt_id = uuid4()
            session.add(
                ScreeningAttempt(
                    attempt_id=attempt_id,
                    agent_id=agent.agent_id,
                    policy_version=13,
                    status="quarantined",
                    screener_hotkey="screener",
                    started_at=_TIME,
                    deadline=_TIME + timedelta(minutes=30),
                )
            )
            await session.flush()
            finding = SourceReviewFinding(
                artifact_sha256=agent.sha256,
                prompt_revision="source-review-test",
                risk_level="high",
                confidence=0.9,
                categories=["mechanism"],
                evidence=[{"path": "src/old.rs", "line": 3, "category": "mechanism"}],
                summary="mechanism location",
            )
            session.add(
                ScreeningQuarantine(
                    quarantine_id=uuid4(),
                    agent_id=agent.agent_id,
                    attempt_id=attempt_id,
                    screener_hotkey="screener",
                    policy_version=13,
                    manifest_digest="b" * 64,
                    reason_code="source-review-mechanism",
                    status="resolved",
                    created_at=_TIME,
                    resolved_at=_TIME,
                    resolved_by="reviewer",
                    resolution="reject",
                    resolution_reason="mechanism proven",
                    finding=finding.model_dump(mode="json"),
                    finding_digest="c" * 64 if tampered else finding.canonical_digest(),
                )
            )
            if not tampered:
                valid = agent.agent_id
        candidate = await seed(
            session,
            hotkey="candidate",
            time=_TIME + timedelta(days=1),
            status=AgentStatus.UPLOADED,
        )
        rows = await rejected_ancestors(session, candidate=candidate)
        assert len(rows) == 1
        assert rows[0].agent_id == valid
        assert rows[0].references == ("src/old.rs:3",)
