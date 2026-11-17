"""SN-489: a canonical score that measured a provider outage is voided.

Fixture: the 2026-11-17 openrouter credit outage (circuit epoch eeb175b7,
12:06:45Z to 13:21:31Z, every failure ``upstream_http_402``). Agent dba51d92
was scored by three validators; 5CqJAjSj's run ebed9c89 (seed
5406736405836416917) finished at 12:13:26Z with 1071 of its 1473 chat calls
refused and composite 0.6312, against 0.763616 and 0.809086 from the other
two. The finalized median 0.763616 counted it. Sweep 291ff099 at 12:17:30Z
flagged it and voided nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.ticket_status import TicketPurpose, TicketStatus
from ditto.db.models import (
    Agent,
    AgentStatus,
    InferenceGrant,
    InferenceRequest,
    Score,
    ScoreAuditEntry,
    ValidatorTicket,
)
from ditto.db.queries.audit import (
    EVENT_SCORE_RETEST_QUEUED,
    EVENT_SCORE_RETEST_RELEASED,
    EVENT_SCORE_RETEST_REQUESTED,
    append_audit_entry,
    get_latest_score_retest_event,
)
from ditto.db.queries.canonical_outage_voids import (
    MAX_AUTOMATIC_OUTAGE_RETESTS,
    CanonicalVoidOutcome,
    CanonicalVoidRequest,
    is_canonical_run,
    outage_void_request_id,
    void_canonical_scores,
)
from ditto.db.queries.outage_sweep import (
    SWEEP_ACTOR,
    SWEEP_CANONICAL_REASON,
    sweep_outage_voids,
)
from ditto.db.queries.score_retests import (
    OUTAGE_VOID_RETEST_BASIS,
    activate_next_score_retest,
)

pytestmark = pytest.mark.asyncio

V = 18
AGENT = UUID("dba51d92-a0eb-4e97-8a33-9d06016c3fbd")
CF = "5CFtzzb4vym9eysfeF9cxxp6D7gksuUVTKYNq1mchnrMs118"
CQ = "5CqJAjSjv8fjF9uAQpDLyfN1hZEvBjwpFgcGeLbYpcbSaD1C"
HT = "5HT9U1J85fyNVudFAHuhYSGajRth8r3kYo7hTvkUXFEEDhQD"
CQ_RUN = "ebed9c89-c1d8-4a7a-9470-c5b3251ddd8e"
CQ_SEED = 5_406_736_405_836_416_917
CQ_COMPOSITE = 0.6312
CQ_ACCEPTED = datetime(2026, 11, 17, 12, 13, 26, tzinfo=UTC)
CIRCUIT_OPENED = datetime(2026, 11, 17, 12, 6, 45, tzinfo=UTC)
SWEEP_AT = datetime(2026, 11, 17, 12, 17, 30, tzinfo=UTC)
CHAT_CALLS = 1473
REFUSED_402 = 1071
SCORES = {
    CF: ("3482239a-90ca-4d18-82de-96c261ea0615", 1_274_806_115_367_968_219, 0.763616),
    CQ: (CQ_RUN, CQ_SEED, CQ_COMPOSITE),
    HT: ("e32daac1-302c-4cff-821d-defcaa4c1a38", 4_137_275_559_037_336_595, 0.809086),
}
REASON = "Nov 17 openrouter 402 outage measured the provider, not the agent"


def _grant(validator: str, created_at: datetime) -> InferenceGrant:
    return InferenceGrant(
        grant_id=uuid4(),
        agent_id=AGENT,
        bench_version=V,
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


def _call(
    grant: InferenceGrant,
    *,
    status: str,
    started_at: datetime,
    code: str | None = None,
) -> InferenceRequest:
    return InferenceRequest(
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
        started_at=started_at,
        completed_at=started_at,
    )


def _score(validator: str, accepted_at: datetime) -> Score:
    run_id, seed, composite = SCORES[validator]
    return Score(
        agent_id=AGENT,
        validator_hotkey=validator,
        bench_version=V,
        run_id=run_id,
        seed=seed,
        composite=composite,
        tool_mean=0.94,
        memory_mean=0.96,
        median_ms=2900,
        n=350,
        generated_at=accepted_at,
        created_at=accepted_at,
        updated_at=accepted_at,
    )


async def seed_nov17(
    session: AsyncSession, *, status: AgentStatus = AgentStatus.SCORED
) -> None:
    """Three scored validators; only 5CqJAjSj's grant measured the outage."""
    agent = Agent(
        agent_id=AGENT,
        miner_hotkey="5D2qgKgG3xZfq38FKS1yPqShU2ED6ZJ4QTcinnTrkNaK6YoH",
        name="wren-mem",
        sha256=uuid4().hex * 2,
        status=status,
        created_at=CQ_ACCEPTED - timedelta(hours=12),
    )
    accepted = {
        CF: CQ_ACCEPTED - timedelta(minutes=40),
        CQ: CQ_ACCEPTED,
        HT: CQ_ACCEPTED - timedelta(hours=8),
    }
    grants = {
        validator: _grant(validator, at - timedelta(minutes=50))
        for validator, at in accepted.items()
    }
    outage = grants[CQ]
    span = (CQ_ACCEPTED - CIRCUIT_OPENED) / REFUSED_402
    async with session.begin():
        session.add(agent)
        await session.flush()
        session.add_all(
            [
                ValidatorTicket(
                    agent_id=AGENT,
                    validator_hotkey=validator,
                    slot_id="slot-0",
                    status=TicketStatus.SCORED,
                    purpose=TicketPurpose.CANONICAL_QUORUM,
                    purpose_revision=1,
                    legacy_completion_allowed=False,
                    issued_at=at - timedelta(minutes=50),
                    deadline=at + timedelta(minutes=40),
                    bench_version=V,
                    attempt_count=1,
                )
                for validator, at in accepted.items()
            ]
        )
        await session.flush()
        session.add_all(list(grants.values()))
        await session.flush()
        session.add_all(
            [
                *(
                    _call(outage, status="completed", started_at=outage.created_at)
                    for _ in range(CHAT_CALLS - REFUSED_402)
                ),
                *(
                    _call(
                        outage,
                        status="failed",
                        started_at=CIRCUIT_OPENED + span * index,
                        code="upstream_http_402",
                    )
                    for index in range(REFUSED_402)
                ),
                *(
                    _call(grant, status="completed", started_at=grant.created_at)
                    for validator, grant in grants.items()
                    if validator != CQ
                    for _ in range(600)
                ),
                *(_score(validator, at) for validator, at in accepted.items()),
            ]
        )


def _request(
    validator: str = CQ,
    *,
    run_id: str | None = None,
    composite: float | None = None,
    seed: int | None = None,
) -> CanonicalVoidRequest:
    expected_run, expected_seed, expected_composite = SCORES[validator]
    return CanonicalVoidRequest(
        agent_id=AGENT,
        validator_hotkey=validator,
        expected_run_id=run_id or expected_run,
        expected_composite=expected_composite if composite is None else composite,
        expected_seed=expected_seed if seed is None else seed,
    )


async def _void(
    session_maker: async_sessionmaker[AsyncSession],
    *requests: CanonicalVoidRequest,
    apply: bool,
    source: str = "operator",
) -> list[CanonicalVoidOutcome]:
    async with session_maker() as session, session.begin():
        return await void_canonical_scores(
            session,
            requests=list(requests),
            bench_version=V,
            reason=REASON,
            actor="operator@example",
            source=source,  # type: ignore[arg-type]
            now=SWEEP_AT,
            apply=apply,
        )


async def _queued(
    session_maker: async_sessionmaker[AsyncSession],
) -> list[ScoreAuditEntry]:
    async with session_maker() as read:
        return list(
            await read.scalars(
                select(ScoreAuditEntry)
                .where(ScoreAuditEntry.event == EVENT_SCORE_RETEST_QUEUED)
                .order_by(ScoreAuditEntry.seq)
            )
        )


async def _median(session_maker: async_sessionmaker[AsyncSession]) -> float:
    async with session_maker() as read:
        composites = sorted(
            await read.scalars(
                select(Score.composite).where(
                    Score.agent_id == AGENT, Score.bench_version == V
                )
            )
        )
    return composites[len(composites) // 2]


async def test_dry_run_reports_the_402_evidence_and_writes_nothing(
    session_maker: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    await seed_nov17(session)

    (outcome,) = await _void(session_maker, _request(), apply=False)

    assert outcome.disposition == "ready", outcome.detail
    assert outcome.seed == CQ_SEED
    assert outcome.request_id == outage_void_request_id(
        agent_id=AGENT, bench_version=V, validator_hotkey=CQ, run_id=CQ_RUN
    )
    evidence = outcome.evidence
    assert evidence["sample_kind"] == "canonical"
    assert (evidence["run_id"], evidence["composite"]) == (CQ_RUN, CQ_COMPOSITE)
    assert evidence["seed"] == str(CQ_SEED)
    assert evidence["chat_requests"] == CHAT_CALLS
    assert evidence["failed_requests"] == REFUSED_402
    assert evidence["outage_failed_requests"] == REFUSED_402
    assert evidence["failure_codes"] == {"upstream_http_402": REFUSED_402}
    assert outcome.evidence_digest is not None
    assert await _queued(session_maker) == []
    assert await _median(session_maker) == pytest.approx(0.763616)


async def test_apply_queues_one_same_validator_replacement_and_is_idempotent(
    session_maker: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    await seed_nov17(session)

    (applied,) = await _void(session_maker, _request(), apply=True)
    (again,) = await _void(session_maker, _request(), apply=True)

    assert applied.disposition == "applied"
    assert again.disposition == "already_queued"
    assert again.request_id == applied.request_id
    (entry,) = await _queued(session_maker)
    assert (entry.agent_id, entry.validator_hotkey) == (AGENT, CQ)
    payload = entry.payload
    assert payload["request_id"] == str(applied.request_id)
    assert payload["basis"] == OUTAGE_VOID_RETEST_BASIS
    assert (payload["source"], payload["automatic"]) == ("operator", False)
    assert (payload["run_id"], payload["bench_version"]) == (CQ_RUN, V)
    assert payload["preserved_score"]["composite"] == CQ_COMPOSITE
    assert payload["preserved_score"]["seed"] == str(CQ_SEED)
    assert payload["evidence"] == applied.evidence
    assert payload["evidence_digest"] == applied.evidence_digest
    assert payload["outage_started_at"] == CIRCUIT_OPENED.isoformat()
    # The score stays canonical until its replacement lands.
    assert await _median(session_maker) == pytest.approx(0.763616)
    async with session_maker() as read:
        assert await is_canonical_run(
            read, agent_id=AGENT, bench_version=V, validator_hotkey=CQ, run_id=CQ_RUN
        )


async def test_clean_canonical_scores_do_not_qualify(
    session_maker: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    await seed_nov17(session)

    outcomes = await _void(session_maker, _request(CF), _request(HT), apply=True)

    assert [o.disposition for o in outcomes] == [
        "insufficient_evidence",
        "insufficient_evidence",
    ]
    assert outcomes[0].evidence["chat_requests"] == 600
    assert outcomes[0].evidence["outage_failed_requests"] == 0
    assert await _queued(session_maker) == []


async def test_guards_refuse_stale_or_unknown_requests(
    session_maker: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    await seed_nov17(session)

    outcomes = await _void(
        session_maker,
        _request(composite=0.7),
        _request(seed=1),
        _request(run_id="some-other-run"),
        CanonicalVoidRequest(
            agent_id=AGENT,
            validator_hotkey="5Unknown",
            expected_run_id=CQ_RUN,
            expected_composite=CQ_COMPOSITE,
        ),
        apply=True,
    )

    assert [o.disposition for o in outcomes] == [
        "stale_guard",
        "stale_guard",
        "stale_guard",
        "not_found",
    ]
    assert await _queued(session_maker) == []


async def test_unscoreable_agent_and_open_retest_are_refused(
    session_maker: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    await seed_nov17(session)
    async with session_maker() as write, write.begin():
        await append_audit_entry(
            write,
            agent_id=AGENT,
            validator_hotkey=CQ,
            event=EVENT_SCORE_RETEST_QUEUED,
            payload={
                "request_id": str(uuid4()),
                "basis": "dueling_season_rotation",
                "bench_version": V,
                "run_id": CQ_RUN,
            },
            recorded_at=SWEEP_AT,
        )
    (busy,) = await _void(session_maker, _request(), apply=True)
    assert busy.disposition == "retest_busy"

    # Close that re-test, then reject the agent: the evidence still qualifies,
    # but there is nothing left to re-score.
    async with session_maker() as write, write.begin():
        await append_audit_entry(
            write,
            agent_id=AGENT,
            validator_hotkey=CQ,
            event=EVENT_SCORE_RETEST_RELEASED,
            payload={"request_id": "closed", "bench_version": V, "reason": "test"},
            recorded_at=SWEEP_AT,
        )
        agent = await write.get(Agent, AGENT)
        assert agent is not None
        agent.status = AgentStatus.REJECTED
    (refused,) = await _void(session_maker, _request(), apply=True)
    assert refused.disposition == "not_retestable"
    assert "rejected" in (refused.detail or "")
    assert len(await _queued(session_maker)) == 1


async def test_sweep_replays_nov17_and_queues_the_flagged_canonical_score(
    session_maker: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    await seed_nov17(session)

    async with session_maker() as write, write.begin():
        run = await sweep_outage_voids(write, now=SWEEP_AT, bench_version=V)
    assert run is not None

    # No continual-retest sample to void: the void is the canonical one.
    assert run.voided_count == 0
    (flag,) = run.canonical_flagged
    assert (flag["agent_id"], flag["validator_hotkey"], flag["run_id"]) == (
        str(AGENT),
        CQ,
        CQ_RUN,
    )
    assert (flag["chat_requests"], flag["outage_failed_requests"]) == (
        CHAT_CALLS,
        REFUSED_402,
    )
    assert flag["void_disposition"] == "applied"
    assert flag["void_request_id"] == str(
        outage_void_request_id(
            agent_id=AGENT, bench_version=V, validator_hotkey=CQ, run_id=CQ_RUN
        )
    )
    (entry,) = await _queued(session_maker)
    assert (entry.payload["actor"], entry.payload["reason"]) == (
        SWEEP_ACTOR,
        SWEEP_CANONICAL_REASON,
    )
    assert (entry.payload["source"], entry.payload["automatic"]) == (
        "outage_sweep",
        True,
    )

    # The next run starts past the watermark and queues nothing new.
    async with session_maker() as write, write.begin():
        later = await sweep_outage_voids(
            write, now=SWEEP_AT + timedelta(minutes=5), bench_version=V
        )
    assert later is not None and later.canonical_flagged == []
    assert len(await _queued(session_maker)) == 1


async def test_sweep_stops_queueing_after_the_automatic_cap(
    session_maker: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    await seed_nov17(session)
    async with session_maker() as write, write.begin():
        for _ in range(MAX_AUTOMATIC_OUTAGE_RETESTS):
            await append_audit_entry(
                write,
                agent_id=AGENT,
                validator_hotkey=CQ,
                event=EVENT_SCORE_RETEST_QUEUED,
                payload={
                    "request_id": str(uuid4()),
                    "basis": OUTAGE_VOID_RETEST_BASIS,
                    "source": "outage_sweep",
                    "bench_version": V,
                    "run_id": "earlier-outage-run",
                },
                recorded_at=SWEEP_AT - timedelta(hours=1),
            )
            await append_audit_entry(
                write,
                agent_id=AGENT,
                validator_hotkey=CQ,
                event=EVENT_SCORE_RETEST_RELEASED,
                payload={"request_id": "closed", "bench_version": V, "reason": "x"},
                recorded_at=SWEEP_AT - timedelta(hours=1),
            )

    async with session_maker() as write, write.begin():
        run = await sweep_outage_voids(write, now=SWEEP_AT, bench_version=V)
    assert run is not None
    (flag,) = run.canonical_flagged
    assert flag["void_disposition"] == "auto_cap_reached"
    # An operator is not capped.
    (operator,) = await _void(session_maker, _request(), apply=False)
    assert operator.disposition == "ready"


async def _promote(session_maker: async_sessionmaker[AsyncSession], at: datetime):
    async with session_maker() as write, write.begin():
        ticket = await activate_next_score_retest(
            write,
            validator_hotkey=CQ,
            now=at,
            supports_version=lambda version: version == V,
            slot_id="slot-0",
            required_basis=OUTAGE_VOID_RETEST_BASIS,
            allow_parallel_ordinary=True,
            allow_parallel_contract_retests=True,
        )
        if ticket is None:
            return None
        return (ticket.status, ticket.attempt_count, ticket.purpose_revision)


async def test_promotion_reissues_the_ticket_without_spending_retry_budget(
    session_maker: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    await seed_nov17(session)
    await _void(session_maker, _request(), apply=True)

    promoted = await _promote(session_maker, SWEEP_AT + timedelta(minutes=1))

    assert promoted == (TicketStatus.ISSUED, 1, 2)
    async with session_maker() as read:
        latest = await get_latest_score_retest_event(
            read, agent_id=AGENT, validator_hotkey=CQ
        )
    assert latest is not None and latest.event == EVENT_SCORE_RETEST_REQUESTED
    assert latest.payload["basis"] == OUTAGE_VOID_RETEST_BASIS


async def test_promotion_runs_beside_other_work_on_a_free_slot(
    session_maker: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    """The replacement does not wait for the validator to go idle."""
    await seed_nov17(session)
    other = uuid4()
    async with session_maker() as write, write.begin():
        write.add(
            Agent(
                agent_id=other,
                miner_hotkey="5Other",
                name="other",
                sha256=uuid4().hex * 2,
                status=AgentStatus.EVALUATING,
                created_at=SWEEP_AT - timedelta(hours=1),
            )
        )
        await write.flush()
        write.add(
            ValidatorTicket(
                agent_id=other,
                validator_hotkey=CQ,
                slot_id="slot-1",
                status=TicketStatus.ISSUED,
                purpose=TicketPurpose.CANONICAL_QUORUM,
                purpose_revision=1,
                legacy_completion_allowed=False,
                issued_at=SWEEP_AT,
                deadline=SWEEP_AT + timedelta(hours=2),
                bench_version=V,
                attempt_count=1,
            )
        )
    await _void(session_maker, _request(), apply=True)

    assert await _promote(session_maker, SWEEP_AT + timedelta(minutes=1)) == (
        TicketStatus.ISSUED,
        1,
        2,
    )


async def test_failed_replacement_restores_the_ticket_and_closes(
    session_maker: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    await seed_nov17(session)
    await _void(session_maker, _request(), apply=True)
    await _promote(session_maker, SWEEP_AT + timedelta(minutes=1))
    # fail_job flips the reused ticket to expired; the lifecycle stays requested.
    async with session_maker() as write, write.begin():
        ticket = await write.get(ValidatorTicket, (AGENT, V, CQ))
        assert ticket is not None
        ticket.status = TicketStatus.EXPIRED

    assert await _promote(session_maker, SWEEP_AT + timedelta(minutes=30)) is None

    async with session_maker() as read:
        ticket = await read.get(ValidatorTicket, (AGENT, V, CQ))
        latest = await get_latest_score_retest_event(
            read, agent_id=AGENT, validator_hotkey=CQ
        )
    assert ticket is not None and ticket.status == TicketStatus.SCORED
    assert ticket.attempt_count == 1
    assert latest is not None and latest.event == EVENT_SCORE_RETEST_RELEASED
    (repeat,) = await _void(session_maker, _request(), apply=True)
    assert repeat.disposition == "retest_closed"
    assert await _median(session_maker) == pytest.approx(0.763616)


async def test_an_agent_short_of_quorum_cannot_finalize_on_the_outage_run(
    session_maker: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    await seed_nov17(session, status=AgentStatus.EVALUATING)

    (applied,) = await _void(session_maker, _request(), apply=True)
    assert applied.disposition == "applied"

    # Re-issued, the ticket no longer counts as a scored quorum slot.
    assert await _promote(session_maker, SWEEP_AT + timedelta(minutes=1)) == (
        TicketStatus.ISSUED,
        1,
        2,
    )
    async with session_maker() as read:
        scored = await read.scalar(
            select(func.count())
            .select_from(ValidatorTicket)
            .where(
                ValidatorTicket.agent_id == AGENT,
                ValidatorTicket.status == TicketStatus.SCORED,
            )
        )
    assert scored == 2


async def test_row_left_expired_by_a_continual_retest_is_re_leased(
    session_maker: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    """Live shape on 2026-11-17: after the canonical run, a continual retest
    leased the same row (wave seed pinned) and expired at 14:36:51Z, so the
    accepted score's row reads ``expired`` and replace_validator_score refuses
    it. The outage void still re-leases it on the canonical seed."""
    await seed_nov17(session)
    async with session_maker() as write, write.begin():
        ticket = await write.get(ValidatorTicket, (AGENT, V, CQ))
        assert ticket is not None
        ticket.status = TicketStatus.EXPIRED
        ticket.purpose = TicketPurpose.CONTINUAL_RETEST
        ticket.purpose_revision = 2
        ticket.seed = 777
        ticket.attempt_count = 2
        ticket.deadline = datetime(2026, 11, 17, 14, 36, 51, tzinfo=UTC)
        ticket.retry_after = datetime(2026, 11, 17, 20, 0, tzinfo=UTC)

    (dry,) = await _void(session_maker, _request(), apply=False)
    assert dry.disposition == "ready", dry.detail
    (applied,) = await _void(session_maker, _request(), apply=True)
    assert applied.disposition == "applied"

    assert await _promote(session_maker, SWEEP_AT + timedelta(hours=3)) == (
        TicketStatus.ISSUED,
        2,
        3,
    )
    async with session_maker() as read:
        ticket = await read.get(ValidatorTicket, (AGENT, V, CQ))
    assert ticket is not None
    assert ticket.purpose == TicketPurpose.CANONICAL_QUORUM
    # The wave seed is dropped; the job re-derives the canonical seed.
    assert ticket.seed is None
    assert ticket.retry_after is None
