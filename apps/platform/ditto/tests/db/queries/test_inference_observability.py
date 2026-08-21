"""Runtime-metrics ledger reads: correct on a seeded hour, bounded on a full one.

``load_inference_runtime_rows`` is what ``/admin/inference-runtime-metrics``
shows Backroom. These tests pin two things the 2026-08-21 incident proved were
unguarded: the statements' *meaning* (peaks are a running concurrency count,
windows are cumulative, the stale count sees every stuck request), so the
rewrite that made them cheap changed no number; and the partial index the
stale count relies on, so the migration cannot silently go missing.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.ticket_status import TicketStatus
from ditto.db.models import (
    Agent,
    AgentStatus,
    InferenceGrant,
    InferenceRequest,
    ValidatorTicket,
)
from ditto.db.queries.inference_observability import (
    CURRENT_SQL,
    WINDOWS_SECONDS,
    load_inference_runtime_rows,
)

pytestmark = pytest.mark.asyncio

_BENCH_VERSION = 7
_STALE_AFTER = 180


async def _grant(
    session: AsyncSession,
    *,
    validator_hotkey: str,
    now: datetime,
    slot_id: str = "slot-0",
    status: str = "active",
) -> InferenceGrant:
    agent = Agent(
        agent_id=uuid4(),
        miner_hotkey=f"miner-{uuid4().hex[:8]}",
        name="observability",
        sha256=uuid4().hex * 2,
        status=AgentStatus.EVALUATING,
        created_at=now,
    )
    ticket = ValidatorTicket(
        agent_id=agent.agent_id,
        validator_hotkey=validator_hotkey,
        slot_id=slot_id,
        status=TicketStatus.ISSUED,
        issued_at=now - timedelta(hours=2),
        deadline=now + timedelta(minutes=20),
        bench_version=_BENCH_VERSION,
        attempt_count=1,
    )
    session.add_all([agent, ticket])
    await session.flush()
    grant = InferenceGrant(
        grant_id=uuid4(),
        agent_id=agent.agent_id,
        bench_version=_BENCH_VERSION,
        validator_hotkey=validator_hotkey,
        slot_id=ticket.slot_id,
        ticket_deadline=ticket.deadline,
        status=status,
        bearer_digest=None,
        broker_public_key=None,
        generation=1,
        allowed_models=["test-model"],
        route_provider="test-provider",
        route_profile="openrouter-route-test-v1",
        request_budget=1000,
        token_budget=1_000_000,
        embedding_model="test-embedding",
        embedding_profile="openrouter-embedding-test-v1",
        embedding_provider="test-provider",
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
        expires_at=ticket.deadline,
    )
    session.add(grant)
    await session.flush()
    return grant


def _request(
    grant: InferenceGrant,
    *,
    kind: str,
    started_at: datetime,
    duration: timedelta | None,
    status: str = "completed",
    tokens: tuple[int, int] = (10, 5),
    timed_out: bool = False,
) -> InferenceRequest:
    completed_at = None if duration is None else started_at + duration
    return InferenceRequest(
        grant_id=grant.grant_id,
        nonce=uuid4(),
        generation=grant.generation,
        status=status,
        request_kind=kind,
        model="test-model",
        reserved_tokens=16,
        prompt_tokens=tokens[0],
        completion_tokens=tokens[1],
        cost_microusd=0,
        timed_out=timed_out,
        latency_ms=(
            None if completed_at is None else int(duration.total_seconds() * 1000)  # type: ignore[union-attr]
        ),
        started_at=started_at,
        completed_at=completed_at,
    )


def _peak(intervals: list[tuple[datetime, datetime]]) -> int:
    """Reference running-concurrency peak, ends netted before starts on ties."""
    events = sorted(
        [(start, 1) for start, _ in intervals] + [(end, -1) for _, end in intervals],
        key=lambda event: (event[0], event[1]),
    )
    active = peak = 0
    for _, delta in events:
        active += delta
        peak = max(peak, active)
    return peak


async def test_inflight_partial_index_is_present_and_valid(
    session: AsyncSession,
) -> None:
    """The unbounded stale count is only affordable on this index."""
    row = (
        await session.execute(
            text(
                "SELECT i.indisvalid, pg_get_indexdef(i.indexrelid) "
                "  FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                " WHERE c.relname = 'inference_requests_inflight_idx'"
            )
        )
    ).one()
    assert row[0] is True
    definition = row[1]
    assert "(request_kind, started_at)" in definition
    assert "WHERE (status = 'started'::text)" in definition


async def test_windows_and_peaks_reproduce_the_seeded_hour(
    session: AsyncSession,
) -> None:
    """Every number the endpoint renders, recomputed in Python from the seed.

    Requests sit well inside or outside each window edge (no request starts
    within 5 s of a boundary), so clock drift between seeding and reading
    cannot move one across a window.
    """
    async with session.begin():
        now = datetime.now(UTC)
        validator_a = await _grant(session, validator_hotkey="validator-a", now=now)
        validator_a2 = await _grant(
            session, validator_hotkey="validator-a", now=now, slot_id="slot-1"
        )
        validator_b = await _grant(session, validator_hotkey="validator-b", now=now)
        validator_b2 = await _grant(
            session, validator_hotkey="validator-b", now=now, slot_id="slot-1"
        )
        seconds = timedelta(seconds=1)
        chat: list[InferenceRequest] = [
            # Three overlapping on grant A inside the 60 s window.
            _request(
                validator_a,
                kind="chat",
                started_at=now - 40 * seconds,
                duration=20 * seconds,
            ),
            _request(
                validator_a,
                kind="chat",
                started_at=now - 38 * seconds,
                duration=20 * seconds,
            ),
            _request(
                validator_a,
                kind="chat",
                started_at=now - 36 * seconds,
                duration=20 * seconds,
            ),
            # One on validator A's second grant overlapping those three: the
            # validator peak is 4 while the ticket peak stays 3.
            _request(
                validator_a2,
                kind="chat",
                started_at=now - 37 * seconds,
                duration=10 * seconds,
            ),
            # Validator B, still open, 10 minutes in (timed out, no latency).
            _request(
                validator_b,
                kind="chat",
                started_at=now - 600 * seconds,
                duration=None,
                status="started",
                timed_out=True,
            ),
            # Failed and canceled rows inside the 15 min window.
            _request(
                validator_b,
                kind="chat",
                started_at=now - 700 * seconds,
                duration=5 * seconds,
                status="failed",
            ),
            _request(
                validator_b,
                kind="chat",
                started_at=now - 800 * seconds,
                duration=5 * seconds,
                status="canceled",
            ),
            # Six overlapping on validator B 40 minutes ago, three per grant:
            # the hour's global and validator peak is 6, the ticket peak 3.
            *(
                _request(
                    grant,
                    kind="chat",
                    started_at=now - (2400 + i) * seconds,
                    duration=60 * seconds,
                )
                for i in range(3)
                for grant in (validator_b, validator_b2)
            ),
            # Older than the hour: invisible to every window.
            _request(
                validator_b,
                kind="chat",
                started_at=now - 4000 * seconds,
                duration=5 * seconds,
            ),
        ]
        embedding: list[InferenceRequest] = [
            _request(
                validator_a,
                kind="embedding",
                started_at=now - 30 * seconds,
                duration=1 * seconds,
                tokens=(7, 0),
            ),
            _request(
                validator_a,
                kind="embedding",
                started_at=now - 30 * seconds,
                duration=1 * seconds,
                tokens=(7, 0),
            ),
            _request(
                validator_b,
                kind="embedding",
                started_at=now - 1200 * seconds,
                duration=2 * seconds,
                tokens=(7, 0),
            ),
        ]
        session.add_all(chat + embedding)

    async with session.begin():
        current, windows, peaks = await load_inference_runtime_rows(
            session, stale_after_seconds=_STALE_AFTER
        )
    read_at = datetime.now(UTC)

    def visible(kind: str, window: int) -> list[InferenceRequest]:
        return [
            r
            for r in (chat if kind == "chat" else embedding)
            if r.started_at >= now - timedelta(seconds=window)
        ]

    def interval(r: InferenceRequest) -> tuple[datetime, datetime]:
        return (r.started_at, r.completed_at or read_at)

    assert [(int(w["window_seconds"]), w["request_kind"]) for w in windows] == [
        (window, kind) for window in WINDOWS_SECONDS for kind in ("chat", "embedding")
    ]
    for row in windows:
        kind, window = str(row["request_kind"]), int(row["window_seconds"])
        rows = visible(kind, window)
        latencies = sorted(r.latency_ms for r in rows if r.latency_ms is not None)
        assert int(row["calls"]) == len(rows), (kind, window)
        assert float(row["calls_per_second"]) == pytest.approx(len(rows) / window)
        assert int(row["tokens"]) == sum(
            r.prompt_tokens + r.completion_tokens for r in rows
        )
        assert int(row["completed"]) == sum(r.status == "completed" for r in rows)
        assert int(row["failed"]) == sum(r.status == "failed" for r in rows)
        assert int(row["canceled"]) == sum(r.status == "canceled" for r in rows)
        assert int(row["timed_out"]) == sum(r.timed_out for r in rows)
        assert float(row["latency_p50_ms"]) == pytest.approx(
            _percentile(latencies, 0.5)
        )
        assert float(row["latency_p95_ms"]) == pytest.approx(
            _percentile(latencies, 0.95)
        )
        assert row["latency_max_ms"] == max(latencies)
        assert int(row["peak_global_concurrency"]) == _peak([interval(r) for r in rows])

    hour_chat = visible("chat", 3600)
    assert [
        int(w["peak_global_concurrency"])
        for w in windows
        if w["request_kind"] == "chat"
    ] == [4, 4, 5, 6]  # A's 3 + A2's 1; 900 s adds B's open one; B's six
    assert _peak([interval(r) for r in hour_chat]) == 6

    by_scope = {
        (str(p["scope"]), str(p["request_kind"])): int(p["peak"]) for p in peaks
    }
    grants = {
        g.grant_id: g for g in (validator_a, validator_a2, validator_b, validator_b2)
    }
    for kind, rows in (("chat", hour_chat), ("embedding", visible("embedding", 3600))):
        per_grant = [
            _peak([interval(r) for r in rows if r.grant_id == grant_id])
            for grant_id in grants
        ]
        per_validator = [
            _peak(
                [
                    interval(r)
                    for r in rows
                    if grants[r.grant_id].validator_hotkey == hotkey
                ]
            )
            for hotkey in {g.validator_hotkey for g in grants.values()}
        ]
        assert by_scope[("ticket", kind)] == max(per_grant), kind
        assert by_scope[("validator", kind)] == max(per_validator), kind
    assert by_scope == {
        ("ticket", "chat"): 3,
        ("ticket", "embedding"): 2,
        ("validator", "chat"): 6,
        ("validator", "embedding"): 2,
    }

    lanes = {str(c["request_kind"]): c for c in current}
    assert set(lanes) == {"chat", "embedding"}
    # Validator B's 10-minute-old started request is the only stale one.
    assert int(lanes["chat"]["stale_started_requests"]) == 1
    assert int(lanes["embedding"]["stale_started_requests"]) == 0
    assert int(lanes["chat"]["live_grants"]) == 4
    assert int(lanes["embedding"]["live_grants"]) == 4


async def test_stale_count_sees_stuck_requests_older_than_any_window(
    session: AsyncSession,
) -> None:
    """The stale count is deliberately unbounded: a request stuck ``started``
    for a day is exactly what it exists to surface, and the partial index is
    what makes that affordable. Rows the revocation sweep already settled do
    not count however old they are."""
    async with session.begin():
        now = datetime.now(UTC)
        grant = await _grant(session, validator_hotkey="validator-stale", now=now)
        session.add_all(
            [
                _request(
                    grant,
                    kind="chat",
                    started_at=now - timedelta(days=1),
                    duration=None,
                    status="started",
                ),
                _request(
                    grant,
                    kind="embedding",
                    started_at=now - timedelta(days=3),
                    duration=None,
                    status="started",
                ),
                _request(
                    grant,
                    kind="chat",
                    started_at=now - timedelta(days=2),
                    duration=None,
                    status="canceled",
                ),
                # Fresh in-flight work is not stale.
                _request(
                    grant,
                    kind="chat",
                    started_at=now - timedelta(seconds=10),
                    duration=None,
                    status="started",
                ),
            ]
        )
    async with session.begin():
        rows = (
            (
                await session.execute(
                    text(CURRENT_SQL), {"stale_after_seconds": _STALE_AFTER}
                )
            )
            .mappings()
            .all()
        )
    stale = {str(r["request_kind"]): int(r["stale_started_requests"]) for r in rows}
    assert stale == {"chat": 1, "embedding": 1}


async def test_stale_count_plan_walks_the_inflight_index(session: AsyncSession) -> None:
    """Against a ledger that is overwhelmingly settled rows, the stale count
    must prefer the partial index -- the shape that keeps it an index walk of
    a few dozen entries on a 15-million-row production table. Seeded and
    ``ANALYZE``d because on an empty table every access path costs the same
    and the planner's pick says nothing."""
    async with session.begin():
        now = datetime.now(UTC)
        grant = await _grant(session, validator_hotkey="validator-plan", now=now)
        session.add(
            _request(
                grant,
                kind="chat",
                started_at=now - timedelta(hours=1),
                duration=None,
                status="started",
            )
        )
        await session.flush()
        await session.execute(
            text(
                """
                INSERT INTO inference_requests (
                    grant_id, nonce, generation, status, request_kind, model,
                    reserved_tokens, prompt_tokens, completion_tokens,
                    cost_microusd, timed_out, started_at, completed_at
                )
                SELECT :grant_id, gen_random_uuid(), 1, 'completed', 'chat',
                       'test-model', 16, 1, 1, 0, false,
                       now() - make_interval(secs => i), now()
                  FROM generate_series(1, 4000) AS i
                """
            ),
            {"grant_id": grant.grant_id},
        )
    async with session.begin():
        await session.execute(text("ANALYZE inference_requests"))
        plan = "\n".join(
            str(line)
            for line in await session.scalars(
                text("EXPLAIN " + CURRENT_SQL), {"stale_after_seconds": _STALE_AFTER}
            )
        )
    assert "inference_requests_inflight_idx" in plan, plan


def _percentile(values: list[int], fraction: float) -> float:
    """``percentile_cont`` as PostgreSQL defines it: linear interpolation."""
    assert values
    position = (len(values) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    return values[lower] + (values[upper] - values[lower]) * (position - lower)
