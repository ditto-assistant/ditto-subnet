"""An infrastructure void removes one sample from every fold and lets it re-run.

Fixture: the 2026-10-31 Wonder v6 shape. One retest's grant failed most of its
chat calls on the gateway's credit refusal; another validator's retest of a
different seed ran clean.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.confirmation_score_voids import (
    VOID_CONFIRMATION_PHRASE,
    InfraVoidItem,
    InfraVoidRequest,
)
from ditto.api_models.ticket_status import TicketStatus
from ditto.api_server.endpoints import admin_confirmation_score_voids as endpoint
from ditto.db.models import (
    Agent,
    AgentStatus,
    ConfirmationScore,
    ConfirmationScoreVoid,
    InferenceGrant,
    InferenceRequest,
    ValidatorTicket,
)
from ditto.db.queries.confirmation_score_voids import (
    VoidRequest,
    outage_evidence_qualifies,
    outstanding_void_rerun_debts,
    void_confirmation_samples,
)
from ditto.db.queries.confirmation_scores import (
    ConfirmationSeedScore,
    append_confirmation_scores,
    confirmation_composites_by_seed,
    confirmation_depths,
    confirmation_history_by_agent,
    lane_seed_universe,
)

pytestmark = pytest.mark.asyncio

_V = 14
_NOW = datetime(2026, 10, 31, 4, 20, tzinfo=UTC)
_HEO = "5HEouuDaDASnWdqDwYtwQATS5z916W9DyDAtK6WyB2jPRReb"
_CF = "5CFtzzb4vym9eysfeF9cxxp6D7gksuUVTKYNq1mchnrMs118"
_BAD_SEED = 2_639_827_674_349_253_378
_GOOD_SEED = 4_656_752_738_245_642_784


def _grant(agent_id: UUID, validator: str, created_at: datetime) -> InferenceGrant:
    return InferenceGrant(
        grant_id=uuid4(),
        agent_id=agent_id,
        bench_version=_V,
        validator_hotkey=validator,
        slot_id="slot-0",
        ticket_deadline=created_at + timedelta(hours=3),
        status="revoked",
        generation=1,
        allowed_models=["openai/gpt-oss-20b"],
        route_provider="openrouter",
        route_profile="openrouter-route-test-v1",
        request_budget=16384,
        token_budget=75_000_000,
        embedding_model="e",
        embedding_profile="openrouter-embedding-test-v1",
        embedding_provider="p",
        embedding_dimensions=768,
        embedding_request_budget=1000,
        embedding_token_budget=1_000_000,
        embedding_request_count=0,
        embedding_tokens=0,
        embedding_cost_microusd=0,
        embedding_active_requests=0,
        request_count=0,
        prompt_tokens=0,
        completion_tokens=0,
        cost_microusd=0,
        active_requests=0,
        expires_at=created_at + timedelta(hours=3),
        created_at=created_at,
        updated_at=created_at,
    )


def _calls(
    grant: InferenceGrant, *, status: str, count: int, code: str | None = None
) -> list[InferenceRequest]:
    return [
        InferenceRequest(
            grant_id=grant.grant_id,
            nonce=uuid4(),
            generation=1,
            status=status,
            request_kind="chat",
            model="openai/gpt-oss-20b",
            reserved_tokens=16,
            prompt_tokens=0,
            completion_tokens=0,
            cost_microusd=0,
            terminal_error_code=code,
            timed_out=False,
            started_at=grant.created_at,
            completed_at=grant.created_at,
        )
        for _ in range(count)
    ]


async def _fixture(session: AsyncSession) -> UUID:
    agent = Agent(
        agent_id=uuid4(),
        miner_hotkey="5HTUbRK5bN7md878oMKByDYp5chz2STqoyW8uwGG9c1vZdLy",
        name="wonder",
        sha256=uuid4().hex * 2,
        status=AgentStatus.SCORED,
        created_at=_NOW - timedelta(hours=4),
    )
    bad = _grant(agent.agent_id, _HEO, _NOW - timedelta(minutes=30))
    good = _grant(agent.agent_id, _CF, _NOW - timedelta(minutes=12))
    async with session.begin():
        session.add(agent)
        await session.flush()
        session.add_all(
            [
                ValidatorTicket(
                    agent_id=agent.agent_id,
                    validator_hotkey=validator,
                    slot_id="slot-0",
                    status=TicketStatus.SCORED,
                    issued_at=_NOW - timedelta(hours=1),
                    deadline=_NOW + timedelta(hours=2),
                    bench_version=_V,
                    attempt_count=2,
                )
                for validator in (_HEO, _CF)
            ]
        )
        await session.flush()
        session.add_all([bad, good])
        await session.flush()
        session.add_all(
            [
                *_calls(bad, status="completed", count=30),
                *_calls(bad, status="failed", count=40, code="upstream_http_402"),
                *_calls(good, status="completed", count=60),
                *_calls(good, status="failed", count=1, code="upstream_http_402"),
                ConfirmationScore(
                    agent_id=agent.agent_id,
                    validator_hotkey=_HEO,
                    bench_version=_V,
                    seed=_BAD_SEED,
                    composite=0.651813,
                    run_id="fd227604",
                    created_at=_NOW - timedelta(minutes=8),
                ),
                ConfirmationScore(
                    agent_id=agent.agent_id,
                    validator_hotkey=_CF,
                    bench_version=_V,
                    seed=_GOOD_SEED,
                    composite=0.919667,
                    run_id="351dd771",
                    created_at=_NOW - timedelta(minutes=2),
                ),
            ]
        )
    return agent.agent_id


def _request(agent_id: UUID, validator: str, seed: int, run: str, composite: float):  # type: ignore[no-untyped-def]
    return VoidRequest(
        agent_id=agent_id,
        validator_hotkey=validator,
        seed=seed,
        expected_run_id=run,
        expected_composite=composite,
    )


@pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
def test_outage_evidence_gate() -> None:
    assert not outage_evidence_qualifies(chat_requests=1932, outage_failed=1)
    assert not outage_evidence_qualifies(chat_requests=1000, outage_failed=19)
    assert outage_evidence_qualifies(chat_requests=1000, outage_failed=20)
    assert outage_evidence_qualifies(chat_requests=60, outage_failed=5)
    assert not outage_evidence_qualifies(chat_requests=60, outage_failed=4)


async def test_void_removes_sample_everywhere_and_rerun_counts(
    session: AsyncSession,
) -> None:
    agent_id = await _fixture(session)

    async with session.begin():
        outcomes = await void_confirmation_samples(
            session,
            requests=[
                _request(agent_id, _HEO, _BAD_SEED, "fd227604", 0.651813),
                _request(agent_id, _CF, _GOOD_SEED, "351dd771", 0.919667),
            ],
            bench_version=_V,
            reason="gateway credit exhaustion 2026-10-31",
            actor="test",
            source="operator",
            now=_NOW,
            apply=True,
        )
    bad, good = outcomes
    assert bad.disposition == "applied"
    assert bad.evidence["outage_failed_requests"] == 40
    assert bad.evidence["failure_codes"] == {"upstream_http_402": 40}
    assert bad.evidence_digest and len(bad.evidence_digest) == 64
    # One stray refusal on a clean run is not an outage.
    assert good.disposition == "insufficient_evidence"

    assert await confirmation_composites_by_seed(
        session, agent_ids=[agent_id], bench_version=_V
    ) == {agent_id: {_GOOD_SEED: 0.919667}}
    history = await confirmation_history_by_agent(
        session, agent_ids=[agent_id], bench_version=_V
    )
    assert [row.seed for row in history[agent_id]] == [_GOOD_SEED]
    assert await confirmation_depths(
        session, agent_ids=[agent_id], bench_version=_V
    ) == {agent_id: 1}
    # The voided seed reads as unscored, which is what reopens it for leasing.
    assert await lane_seed_universe(
        session, agent_ids=[agent_id], bench_version=_V
    ) == (_GOOD_SEED,)
    await session.commit()

    def rerun(run_id: str, composite: float) -> ConfirmationSeedScore:
        return ConfirmationSeedScore(
            agent_id=agent_id,
            validator_hotkey=_HEO,
            seed=_BAD_SEED,
            composite=composite,
            run_id=run_id,
            signature=None,
        )

    async with session.begin():
        # The voided run resent, even on a pinned lease, never returns.
        assert (
            await append_confirmation_scores(
                session,
                rows=[rerun("fd227604", 0.651813)],
                bench_version=_V,
                created_at=_NOW,
                rerun_seeds=frozenset({_BAD_SEED}),
            )
            == 0
        )
        # A legacy multi-seed report cannot reopen a voided key.
        assert (
            await append_confirmation_scores(
                session,
                rows=[rerun("legacy-union", 0.651813)],
                bench_version=_V,
                created_at=_NOW,
            )
            == 0
        )
        # The single-seed re-run lands at generation 1 and is the one that counts.
        assert (
            await append_confirmation_scores(
                session,
                rows=[rerun("rerun-1", 0.931)],
                bench_version=_V,
                created_at=_NOW + timedelta(hours=1),
                rerun_seeds=frozenset({_BAD_SEED}),
            )
            == 1
        )
    generations = (
        await session.execute(
            select(ConfirmationScore.void_generation, ConfirmationScore.run_id)
            .where(
                ConfirmationScore.agent_id == agent_id,
                ConfirmationScore.seed == _BAD_SEED,
            )
            .order_by(ConfirmationScore.void_generation)
        )
    ).all()
    assert [tuple(row) for row in generations] == [(0, "fd227604"), (1, "rerun-1")]
    assert await confirmation_composites_by_seed(
        session, agent_ids=[agent_id], bench_version=_V
    ) == {agent_id: {_BAD_SEED: 0.931, _GOOD_SEED: 0.919667}}
    await session.commit()

    async with session.begin():
        stale, again = await void_confirmation_samples(
            session,
            requests=[
                # The original is gone; its key now holds the re-run.
                _request(agent_id, _HEO, _BAD_SEED, "fd227604", 0.651813),
                _request(agent_id, _CF, _GOOD_SEED, "wrong-run", 0.919667),
            ],
            bench_version=_V,
            reason="replayed request",
            actor="test",
            source="operator",
            now=_NOW,
            apply=True,
        )
    assert stale.disposition == "stale_guard"
    assert again.disposition == "stale_guard"
    assert (
        await session.scalar(select(func.count()).select_from(ConfirmationScoreVoid))
        == 1
    )


async def test_endpoint_dry_run_then_all_or_nothing(session: AsyncSession) -> None:
    agent_id = await _fixture(session)
    item = InfraVoidItem(
        agent_id=agent_id,
        validator_hotkey=_HEO,
        seed=str(_BAD_SEED),
        expected_run_id="fd227604",
        expected_composite=0.651813,
    )
    clean = InfraVoidItem(
        agent_id=agent_id,
        validator_hotkey=_CF,
        seed=str(_GOOD_SEED),
        expected_run_id="351dd771",
        expected_composite=0.919667,
    )
    from ditto.api_server.endpoints import admin_confirmation_score_voids

    async def version(_session: AsyncSession) -> int:
        return _V

    admin_confirmation_score_voids.active_bench_version = version  # type: ignore[assignment]

    preview = await endpoint.void_infra_confirmation_scores(
        InfraVoidRequest(items=[item], reason="credit outage 2026-10-31"),
        None,
        session,
        x_admin_actor="peyton",
    )
    assert preview.dry_run and preview.applied_count == 0
    assert preview.outcomes[0].disposition == "ready"
    await session.commit()

    mixed = await endpoint.void_infra_confirmation_scores(
        InfraVoidRequest(
            items=[item, clean],
            reason="credit outage 2026-10-31",
            dry_run=False,
            confirmation=VOID_CONFIRMATION_PHRASE,
        ),
        None,
        session,
        x_admin_actor="peyton",
    )
    assert mixed.applied_count == 0
    assert [o.disposition for o in mixed.outcomes] == ["ready", "insufficient_evidence"]
    assert (
        await session.scalar(select(func.count()).select_from(ConfirmationScoreVoid))
        == 0
    )
    await session.commit()

    applied = await endpoint.void_infra_confirmation_scores(
        InfraVoidRequest(
            items=[item],
            reason="credit outage 2026-10-31",
            dry_run=False,
            confirmation=VOID_CONFIRMATION_PHRASE,
        ),
        None,
        session,
        x_admin_actor="peyton",
    )
    assert applied.applied_count == 1
    listed = await endpoint.list_infra_confirmation_voids(None, session)
    assert [(v.seed, v.actor, v.source) for v in listed.items] == [
        (str(_BAD_SEED), "peyton", "operator")
    ]


async def test_each_void_owes_one_replacement_from_either_side(
    session: AsyncSession,
) -> None:
    """The debt is per voided seed and settles on one replacement.

    Either the voiding validator re-runs its own key (generation 1), or a
    validator with no row on the seed runs it fresh -- which ``append`` writes
    at generation 0 under that validator's own key. Both settle the debt, and a
    generation-0 row that predates the void does not.
    """
    agent_id = await _fixture(session)
    async with session.begin():
        [outcome] = await void_confirmation_samples(
            session,
            requests=[_request(agent_id, _HEO, _BAD_SEED, "fd227604", 0.651813)],
            bench_version=_V,
            reason="gateway credit exhaustion 2026-10-31",
            actor="outage_sweep",
            source="outage_sweep",
            now=_NOW,
            apply=True,
        )
    assert outcome.disposition == "applied"
    await session.commit()

    async with session.begin():
        [debt] = await outstanding_void_rerun_debts(session, bench_version=_V)
    assert (debt.agent_id, debt.seed, debt.outstanding) == (agent_id, _BAD_SEED, 1)
    assert debt.rerun_validators == frozenset({_HEO})
    assert debt.scored_validators == frozenset({_HEO})
    await session.commit()

    fresh = "5FreshValidatorWithNoRowOnThisSeedXXXXXXXXXXXXXXX"
    async with session.begin():
        # A fresh validator's single-seed run appends at generation 0 for its
        # own key; the pinned lease's rerun_seeds does not get in its way.
        assert (
            await append_confirmation_scores(
                session,
                rows=[
                    ConfirmationSeedScore(
                        agent_id=agent_id,
                        validator_hotkey=fresh,
                        seed=_BAD_SEED,
                        composite=0.925,
                        run_id="fresh-1",
                        signature=None,
                    )
                ],
                bench_version=_V,
                created_at=_NOW + timedelta(minutes=30),
                rerun_seeds=frozenset({_BAD_SEED}),
            )
            == 1
        )
    row = await session.get(ConfirmationScore, (agent_id, _V, fresh, _BAD_SEED, 0))
    assert row is not None
    assert await outstanding_void_rerun_debts(session, bench_version=_V) == []
    assert await confirmation_composites_by_seed(
        session, agent_ids=[agent_id], bench_version=_V
    ) == {agent_id: {_BAD_SEED: 0.925, _GOOD_SEED: 0.919667}}
    await session.commit()

    # An agent that left scored status owes nothing, whatever its voids say.
    async with session.begin():
        agent = await session.get(Agent, agent_id)
        assert agent is not None
        agent.status = AgentStatus.REJECTED
        await session.execute(
            delete(ConfirmationScore).where(ConfirmationScore.validator_hotkey == fresh)
        )
    async with session.begin():
        assert await outstanding_void_rerun_debts(session, bench_version=_V) == []
