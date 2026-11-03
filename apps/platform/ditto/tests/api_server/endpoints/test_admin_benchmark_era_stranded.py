"""The stranded previous-era read lists exactly the work the era boundary excludes.

After an activation, a ``scored`` submission with previous-era scores that is
not a frozen cohort member, not carryover, not first queued in the new era and
not contract-refreshed has no admission path and no active-version dataset.
The read must name every such row, and nothing the active queue can serve.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.api_server.dependencies import get_session
from ditto.db.models import (
    Agent,
    BenchmarkDataset,
    BenchmarkRollout,
    BenchmarkRolloutCarryover,
    BenchmarkRolloutMember,
    EvaluationPayment,
    Score,
    ScreeningAttempt,
)
from ditto.db.queries.benchmark_admission import agent_is_admitted
from ditto.db.queries.benchmark_era_stranded import (
    REASON_OWNER_IN_COHORT,
    REASON_OWNER_OUTSIDE_COHORT,
    stranded_previous_era_agents,
)
from ditto_screening_protocol import SCREENING_FLOOR_POLICY_VERSION

_FROM = 13
_ACTIVE = 14
_ROLLOUT_START = datetime(2026, 10, 28, 0, 30, 0, tzinfo=UTC)
_ACTIVATED_AT = _ROLLOUT_START + timedelta(hours=1)
_BEFORE = _ROLLOUT_START - timedelta(days=1)
_AFTER = _ACTIVATED_AT + timedelta(hours=2)


async def _rollout(session: AsyncSession) -> BenchmarkRollout:
    rollout = BenchmarkRollout(
        rollout_id=uuid4(),
        from_version=_FROM,
        desired_version=_ACTIVE,
        status="activated",
        cohort_size=10,
        created_at=_ROLLOUT_START,
        activated_at=_ACTIVATED_AT,
        rescore_cohort_target=10,
        priority_cohort_target=5,
    )
    session.add(rollout)
    await session.flush()
    return rollout


async def _agent(
    session: AsyncSession,
    *,
    name: str,
    hotkey: str,
    status: AgentStatus = AgentStatus.SCORED,
    created_at: datetime = _BEFORE,
    passed_at: datetime | None = None,
) -> UUID:
    agent_id = uuid4()
    session.add(
        Agent(
            agent_id=agent_id,
            miner_hotkey=hotkey,
            name=name,
            version=1,
            sha256=f"{abs(hash(name)) % (16**64):064x}",
            status=status,
            screening_policy_version=SCREENING_FLOOR_POLICY_VERSION,
            created_at=created_at,
        )
    )
    await session.flush()
    finished = passed_at or created_at + timedelta(minutes=30)
    session.add(
        ScreeningAttempt(
            attempt_id=uuid4(),
            agent_id=agent_id,
            screener_hotkey="5Screener",
            policy_version=SCREENING_FLOOR_POLICY_VERSION,
            status="passed",
            started_at=finished - timedelta(minutes=20),
            deadline=finished + timedelta(minutes=20),
            finished_at=finished,
            build_only=False,
        )
    )
    await session.flush()
    return agent_id


async def _scores(
    session: AsyncSession,
    agent_id: UUID,
    *,
    bench_version: int,
    composites: tuple[float, ...],
) -> None:
    for index, composite in enumerate(composites):
        session.add(
            Score(
                agent_id=agent_id,
                bench_version=bench_version,
                validator_hotkey=f"5Validator-{index}",
                run_id=f"{agent_id}-{bench_version}-{index}",
                signature=None,
                seed=7 + index,
                composite=composite,
                tool_mean=composite,
                memory_mean=composite,
                median_ms=1,
                n=350,
                details={"bench_version": bench_version},
                generated_at=_BEFORE,
            )
        )
    await session.flush()


async def _dataset(session: AsyncSession, agent_id: UUID) -> None:
    session.add(
        BenchmarkDataset(
            agent_id=agent_id,
            bench_version=_ACTIVE,
            seed=7,
            sha256="ab" * 32,
            run_size="full",
            created_at=_ACTIVATED_AT,
        )
    )
    await session.flush()


class TestStrandedPreviousEraRead:
    async def test_lists_only_scored_work_without_an_admission_path(
        self, session: AsyncSession
    ) -> None:
        async with session.begin():
            rollout = await _rollout(session)
            # Frozen cohort member and its newer, weaker sibling (same hotkey).
            member = await _agent(session, name="owner-a", hotkey="5Owner")
            await _scores(session, member, bench_version=_FROM, composites=(0.9,) * 3)
            await _dataset(session, member)
            await _scores(session, member, bench_version=_ACTIVE, composites=(0.8,))
            session.add(
                BenchmarkRolloutMember(
                    rollout_id=rollout.rollout_id,
                    agent_id=member,
                    position=1,
                    frozen_miner_hotkey="5Owner",
                    frozen_composite=0.9,
                )
            )
            sibling = await _agent(
                session,
                name="owner-b",
                hotkey="5Owner",
                created_at=_BEFORE + timedelta(hours=2),
            )
            await _scores(
                session, sibling, bench_version=_FROM, composites=(0.85, 0.86, 0.87)
            )
            # An owner outside the cohort with two finalized submissions.
            outsider_best = await _agent(session, name="out-a", hotkey="5Out")
            await _scores(
                session, outsider_best, bench_version=_FROM, composites=(0.7,) * 3
            )
            outsider_old = await _agent(
                session,
                name="out-b",
                hotkey="5Out",
                created_at=_BEFORE - timedelta(hours=5),
            )
            await _scores(
                session, outsider_old, bench_version=_FROM, composites=(0.6,) * 3
            )
            # Admitted in other ways, so never listed.
            new_era = await _agent(
                session,
                name="new-era",
                hotkey="5New",
                status=AgentStatus.EVALUATING,
                created_at=_AFTER,
            )
            carried = await _agent(session, name="carried", hotkey="5Carry")
            await _scores(session, carried, bench_version=_FROM, composites=(0.5,) * 2)
            await _dataset(session, carried)
            session.add(
                BenchmarkRolloutCarryover(
                    rollout_id=rollout.rollout_id,
                    agent_id=carried,
                    position=1,
                    frozen_score_count=2,
                    frozen_owner_key="hotkey:5Carry",
                    created_at=_ACTIVATED_AT,
                )
            )
            # Rejected previous-era work is not the board's concern.
            rejected = await _agent(
                session, name="rejected", hotkey="5Rej", status=AgentStatus.REJECTED
            )
            await _scores(
                session, rejected, bench_version=_FROM, composites=(0.99,) * 3
            )
            # Scored only in an older era: excluded from the previous era too.
            ancient = await _agent(session, name="ancient", hotkey="5Old")
            await _scores(
                session, ancient, bench_version=_FROM - 1, composites=(0.4,) * 3
            )

        async with session.begin():
            report = await stranded_previous_era_agents(session)
            for agent_id in (sibling, outsider_best, outsider_old):
                assert not await agent_is_admitted(
                    session, bench_version=_ACTIVE, agent_id=agent_id
                )

        assert report.active_bench_version == _ACTIVE
        assert report.rollout is not None
        assert report.rollout.rollout_id == rollout.rollout_id
        ids = [item.agent_id for item in report.items]
        assert ids == [sibling, outsider_best, outsider_old]
        assert new_era not in ids and carried not in ids and member not in ids
        assert report.total == 3
        assert report.owner_count == 2
        assert report.finalized_count == 3
        assert report.owner_in_cohort_count == 1
        assert report.owner_outside_cohort_count == 2
        assert report.truncated is False

        by_id = {item.agent_id: item for item in report.items}
        sibling_row = by_id[sibling]
        assert sibling_row.reason == REASON_OWNER_IN_COHORT
        assert sibling_row.cohort_sibling_agent_id == member
        assert sibling_row.previous_score_count == 3
        assert sibling_row.previous_median_composite == 0.86
        assert sibling_row.has_active_dataset is False
        assert sibling_row.owner_best_stranded is True
        assert by_id[outsider_best].reason == REASON_OWNER_OUTSIDE_COHORT
        assert by_id[outsider_best].cohort_sibling_agent_id is None
        assert by_id[outsider_best].owner_best_stranded is True
        assert by_id[outsider_old].owner_best_stranded is False

    async def test_cohort_sibling_matches_the_payment_coldkey_across_hotkeys(
        self, session: AsyncSession
    ) -> None:
        async with session.begin():
            rollout = await _rollout(session)
            member = await _agent(session, name="hot-a", hotkey="5HotA")
            await _scores(session, member, bench_version=_FROM, composites=(0.9,) * 3)
            session.add(
                BenchmarkRolloutMember(
                    rollout_id=rollout.rollout_id,
                    agent_id=member,
                    position=1,
                    frozen_miner_hotkey="5HotA",
                    frozen_composite=0.9,
                )
            )
            other = await _agent(session, name="hot-b", hotkey="5HotB")
            await _scores(session, other, bench_version=_FROM, composites=(0.8,) * 3)
            for index, (agent_id, hotkey) in enumerate(
                ((member, "5HotA"), (other, "5HotB"))
            ):
                session.add(
                    EvaluationPayment(
                        block_hash=f"0xblock{index}",
                        extrinsic_index=index,
                        agent_id=agent_id,
                        miner_hotkey=hotkey,
                        miner_coldkey="5SharedCold",
                        amount_rao=1,
                        dest_address="5Dest",
                        timestamp=_BEFORE,
                        created_at=_BEFORE,
                    )
                )
            await session.flush()

        async with session.begin():
            report = await stranded_previous_era_agents(session)
        (row,) = report.items
        assert row.agent_id == other
        assert row.owner_key == "coldkey:5SharedCold"
        assert row.reason == REASON_OWNER_IN_COHORT
        assert row.cohort_sibling_agent_id == member

    async def test_empty_without_an_activated_rollout(
        self, session: AsyncSession
    ) -> None:
        async with session.begin():
            agent_id = await _agent(session, name="lonely", hotkey="5Lonely")
            await _scores(session, agent_id, bench_version=_FROM, composites=(0.5,) * 3)

        async with session.begin():
            report = await stranded_previous_era_agents(session)
        assert report.rollout is None
        assert report.items == []
        assert report.total == 0


_TOKEN = "test-admin-token-at-least-32-characters"


async def test_admin_endpoint_is_authenticated_and_wins_over_the_version_path(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    app.state.config = replace(app.state.config, admin_api_token=_TOKEN)

    async def _session() -> AsyncIterator[AsyncSession]:
        async with session_maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session
    async with session_maker() as session, session.begin():
        await _rollout(session)
        stranded = await _agent(session, name="stranded", hotkey="5Stranded")
        await _scores(session, stranded, bench_version=_FROM, composites=(0.7,) * 3)

    path = "/api/v1/admin/benchmark-rollout/stranded-previous-era"
    denied = await client.get(path)
    assert denied.status_code == 401

    response = await client.get(
        path, headers={"Authorization": f"Bearer {_TOKEN}"}, params={"limit": 5}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["active_bench_version"] == _ACTIVE
    assert body["previous_bench_version"] == _FROM
    assert body["rollout"]["status"] == "activated"
    assert body["prev_gen_carryover_enabled"] is False
    assert body["total"] == 1
    assert body["truncated"] is False
    (row,) = body["items"]
    assert row["agent_id"] == str(stranded)
    assert row["reason"] == REASON_OWNER_OUTSIDE_COHORT
    assert row["previous_finalized"] is True
    assert row["has_active_dataset"] is False
