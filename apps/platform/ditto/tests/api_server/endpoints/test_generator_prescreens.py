"""Generator pre-screen batches: settings, opening and expiry.

Pre-screens run a few never-scored generators on a small panel while no round
needs it; their column scores only order the challenger queue. These tests
cover the lane's settings and the batch lifecycle Platform drives itself.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.generator import GeneratorCompetitionSettings
from ditto.api_models.private_dataset import PrivateDatasetRequest
from ditto.api_models.ticket_status import TicketStatus
from ditto.api_server.generator_prescreens import (
    PRESCREEN_INDEX_BASE,
    PRESCREEN_TTL,
    advance_prescreens,
    close_prescreen_if_reported,
    get_prescreen,
    seconds_until_epoch,
)
from ditto.api_server.generator_rounds import ChainPosition, GeneratorRoundLoop
from ditto.db.models import (
    GeneratorPrescreen,
    GeneratorPrescreenAttempt,
    GeneratorPrescreenResult,
    GeneratorRoundCaseVerdict,
    GeneratorSubmission,
    ValidatorTicket,
)
from ditto.tests.api_server.endpoints.test_generator_competition import (
    _ADMIN,
    _SCORER,
    _settings,
)

# The round tests' autouse stand-in for a round-capable validator fleet; a
# batch freezes a bench contract the same way a round does.
from ditto.tests.api_server.endpoints.test_generator_rounds import (  # noqa: F401
    _VALIDATOR,
    _advance,
    _apply_settings,
    _generation,
    _install,
    _matrix_score,
    _patch_lane,
    _patch_panel,
    _seed_agent,
    _seed_generator,
    _signed_fail,
    _signed_job,
    _signed_score,
    round_capable_fleet,
)

_PRESCREENS_URL = "/api/v1/admin/generator-competition/prescreens"


def test_the_lane_ships_off_and_is_bounded() -> None:
    settings = GeneratorCompetitionSettings()
    assert settings.prescreen_enabled is False
    assert (
        settings.prescreen_cases,
        settings.prescreen_panel_size,
        settings.prescreen_columns,
    ) == (16, 3, 4)
    # A revision written before the lane existed reads as off.
    assert (
        GeneratorCompetitionSettings.model_validate(_settings()).prescreen_enabled
        is False
    )
    for field, bad in (
        ("prescreen_cases", 7),
        ("prescreen_cases", 81),
        ("prescreen_panel_size", 0),
        ("prescreen_panel_size", 17),
        ("prescreen_columns", 0),
        ("prescreen_columns", 9),
    ):
        with pytest.raises(ValidationError):
            GeneratorCompetitionSettings.model_validate(_settings(**{field: bad}))


async def _seed_queue(maker: async_sessionmaker[AsyncSession]) -> dict[str, UUID]:
    """Never-scored generators in qualification order: a, b, a's second, c."""
    ids: dict[str, UUID] = {}
    now = datetime.now(UTC)
    for age, (name, owner) in enumerate(
        (("c", "c"), ("a2", "a"), ("b", "b"), ("a", "a")), start=1
    ):
        ids[name] = await _seed_generator(
            maker, hotkey=f"5Prescreen-{name}", owner=f"coldkey:{owner}", salt=name
        )
        async with maker() as session, session.begin():
            row = await session.get(GeneratorSubmission, ids[name])
            assert row is not None
            row.module = f"example.com/{name}"
            row.qualified_at = now - timedelta(hours=age)
    return ids


# The next round opens comfortably after a batch's TTL unless a test says not.
_IDLE = 4 * 3600.0


async def _tick(
    maker: async_sessionmaker[AsyncSession],
    now: datetime | None = None,
    *,
    next_round_in: float | None = _IDLE,
) -> list[str]:
    async with maker() as session, session.begin():
        return await advance_prescreens(
            session, now=now or datetime.now(UTC), next_round_in=next_round_in
        )


async def _batches(maker: async_sessionmaker[AsyncSession]) -> list[GeneratorPrescreen]:
    async with maker() as session:
        return list(
            await session.scalars(
                select(GeneratorPrescreen).order_by(GeneratorPrescreen.prescreen_index)
            )
        )


async def _prescreen_indexes(
    maker: async_sessionmaker[AsyncSession], ids: dict[str, UUID]
) -> dict[str, int | None]:
    async with maker() as session:
        out: dict[str, int | None] = {}
        for name, submission_id in ids.items():
            row = await session.get(GeneratorSubmission, submission_id)
            assert row is not None
            out[name] = row.prescreen_index
        return out


async def _enable(client: httpx.AsyncClient, **overrides: Any) -> None:
    await _apply_settings(
        client,
        validator_rounds_enabled=True,
        prescreen_enabled=True,
        prescreen_columns=2,
        **overrides,
    )


class TestPrescreenBatches:
    async def test_expired_batches_void_even_when_the_chain_is_unreadable(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The loop voids expired batches before it reads the chain, so a
        chain outage never leaves a batch (and its panel tickets) open."""
        chain = _install(app, session_maker)
        await _enable(client)
        agents = [await _seed_agent(session_maker, rank) for rank in range(3)]
        _patch_panel(monkeypatch, agents)
        await _seed_queue(session_maker)
        await _tick(session_maker)
        chain.read_epoch_schedule = AsyncMock(side_effect=RuntimeError("rpc down"))
        loop = GeneratorRoundLoop(app_state=app.state, session_maker=session_maker)
        with pytest.raises(RuntimeError, match="rpc down"):
            await loop.tick(
                now=datetime.now(UTC) + PRESCREEN_TTL + timedelta(minutes=1)
            )
        [batch] = await _batches(session_maker)
        assert batch.status == "void"

    async def test_a_batch_opens_from_the_queue_head_while_idle(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The batch takes the head of the challenger queue, one generator per
        owner, with its own index range, case count and small panel."""
        _install(app, session_maker)
        await _enable(client)
        agents = [await _seed_agent(session_maker, rank) for rank in range(4)]
        _patch_panel(monkeypatch, agents)
        ids = await _seed_queue(session_maker)

        notes = await _tick(session_maker)
        assert notes and "opened with 2 column(s) of 16 cases, panel 3" in notes[-1]
        [batch] = await _batches(session_maker)
        assert batch.prescreen_index >= PRESCREEN_INDEX_BASE
        assert batch.status == "generating"
        assert batch.cases_per_column == 16
        assert [c["submission_id"] for c in batch.columns] == [
            str(ids["a"]),
            str(ids["b"]),
        ]
        assert [p["agent_id"] for p in batch.panel] == [str(a) for a in agents[:3]]
        assert len(batch.seed) == 64
        assert await _prescreen_indexes(session_maker, ids) == {
            "c": None,
            "a2": None,
            "b": batch.prescreen_index,
            "a": batch.prescreen_index,
        }

        # One batch at a time.
        assert await _tick(session_maker) == []
        assert len(await _batches(session_maker)) == 1

        listing = await client.get(_PRESCREENS_URL, headers=_ADMIN)
        assert listing.status_code == 200, listing.text
        body = listing.json()
        assert body["counts_by_status"] == {"generating": 1}
        assert [p["prescreen_index"] for p in body["prescreens"]] == [
            batch.prescreen_index
        ]
        assert [c["counted"] for c in body["prescreens"][0]["columns"]] == [
            False,
            False,
        ]
        assert (await client.get(_PRESCREENS_URL)).status_code == 401

    async def test_nothing_opens_while_off_or_while_a_round_is_in_flight(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install(app, session_maker)
        await _apply_settings(client, validator_rounds_enabled=True)
        agents = [await _seed_agent(session_maker, rank) for rank in range(3)]
        _patch_panel(monkeypatch, agents)
        await _seed_queue(session_maker)
        assert await _tick(session_maker) == []  # the lane ships off

        # A round in flight keeps the panel: the batch waits for it.
        await _advance(session_maker, 9)  # commits round 3
        await _advance(session_maker, 12)  # opens round 3
        await _enable(client, skip_unchanged_rounds=False)
        assert await _tick(session_maker) == []
        assert await _batches(session_maker) == []

    async def test_a_batch_opens_only_when_it_closes_before_the_next_round(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The next round is always committed one round ahead, so it cannot
        be what blocks a batch. A batch opens only when that round opens
        later than the batch's TTL, so the batch always closes first: it
        never holds the panel a round needs or races the round's opening."""
        _install(app, session_maker)
        await _enable(client)
        agents = [await _seed_agent(session_maker, rank) for rank in range(3)]
        _patch_panel(monkeypatch, agents)
        await _seed_queue(session_maker)
        ttl = PRESCREEN_TTL.total_seconds()
        assert await _tick(session_maker, next_round_in=None) == []
        assert await _tick(session_maker, next_round_in=ttl) == []
        assert await _batches(session_maker) == []
        assert "opened" in (await _tick(session_maker, next_round_in=ttl + 60))[-1]

    async def test_an_expired_batch_is_void_and_its_generators_requeue(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install(app, session_maker)
        await _enable(client)
        agents = [await _seed_agent(session_maker, rank) for rank in range(3)]
        _patch_panel(monkeypatch, agents)
        ids = await _seed_queue(session_maker)
        now = datetime.now(UTC)
        await _tick(session_maker, now)
        [first] = await _batches(session_maker)

        later = now + PRESCREEN_TTL + timedelta(minutes=1)
        notes = await _tick(session_maker, later)
        assert notes[0] == f"prescreen {first.prescreen_index}: void (expired)"
        first, second = await _batches(session_maker)
        assert first.status == "void" and "expired" in (first.void_reason or "")
        # Its generators went back to the queue head and opened the next batch.
        assert second.status == "generating"
        assert second.prescreen_index == first.prescreen_index + 1
        assert [c["submission_id"] for c in second.columns] == [
            str(ids["a"]),
            str(ids["b"]),
        ]
        indexes = await _prescreen_indexes(session_maker, ids)
        assert indexes["a"] == indexes["b"] == second.prescreen_index

    async def test_the_round_loop_drives_the_lane_in_its_own_transaction(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install(app, session_maker)
        await _enable(client)
        agents = [await _seed_agent(session_maker, rank) for rank in range(3)]
        _patch_panel(monkeypatch, agents)
        await _seed_queue(session_maker)
        loop = GeneratorRoundLoop(app_state=app.state, session_maker=session_maker)
        notes = await loop._advance_prescreens(datetime.now(UTC), _IDLE)
        assert len(notes) == 1 and "opened" in notes[0]
        assert len(await _batches(session_maker)) == 1

        # A failing pre-screen tick is reported, never raised into the loop.
        async def _fail(*_args: Any, **_kwargs: Any) -> list[str]:
            raise RuntimeError("boom")

        monkeypatch.setattr(
            "ditto.api_server.generator_prescreens.advance_prescreens", _fail
        )
        assert await loop._advance_prescreens(datetime.now(UTC), _IDLE) == [
            "prescreen tick failed: RuntimeError: boom"
        ]


def test_seconds_until_epoch_follows_the_chain_head() -> None:
    """The next epoch fires at next_epoch_block; each further one adds tempo
    blocks of 12 seconds (round_seconds_left's arithmetic)."""
    head = ChainPosition(
        subnet_epoch_index=26290,
        last_epoch_block=9_487_700,
        last_epoch_block_hash="0x" + "00" * 32,
        tempo=360,
        block=9_487_828,
        next_epoch_block=9_488_061,
    )
    assert seconds_until_epoch(head, 26291) == (9_488_061 - 9_487_828) * 12
    assert seconds_until_epoch(head, 26293) == (9_488_061 + 2 * 360 - 9_487_828) * 12
    assert seconds_until_epoch(head, 26290) == 0.0
    no_prediction = ChainPosition(
        subnet_epoch_index=26290,
        last_epoch_block=9_487_700,
        last_epoch_block_hash="0x" + "00" * 32,
        tempo=360,
        block=9_487_828,
    )
    assert seconds_until_epoch(no_prediction, 26291) is None


_ROUND_WORK = "/api/v1/generator-scorer/round-work"


async def _lease(client: httpx.AsyncClient) -> dict[str, Any] | None:
    resp = await client.get(_ROUND_WORK, headers=_SCORER)
    assert resp.status_code == 200, resp.text
    return resp.json()["work"]


async def _report_panel(
    maker: async_sessionmaker[AsyncSession], index: int
) -> GeneratorPrescreen:
    """Every panel agent reports (the validator lane, in the next part), and
    the batch moves to scoring."""
    async with maker() as session, session.begin():
        batch = await get_prescreen(session, index, for_update=True)
        assert batch is not None and batch.status == "running"
        case_ids = [
            ref["case_id"]
            for refs in (batch.column_cases or {}).values()
            for ref in refs
        ]
        for rank, slot in enumerate(batch.panel):
            session.add(
                GeneratorPrescreenResult(
                    prescreen_index=index,
                    agent_id=UUID(slot["agent_id"]),
                    validator_hotkey="5Validator",
                    ticket_deadline=datetime.now(UTC) + timedelta(hours=1),
                    run_id=str(uuid4()),
                    results=[
                        {"case_id": cid, "score": 1.0 if (i + rank) % 2 else 0.0}
                        for i, cid in enumerate(case_ids)
                    ],
                    answered=len(case_ids),
                    faulted=0,
                    signature="ab" * 64,
                )
            )
        await session.flush()
        assert await close_prescreen_if_reported(session, batch)
        return batch


class TestPrescreenScoring:
    async def test_the_scorer_generates_and_scores_a_batch(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A batch rides the round work protocol: its index is the work's
        round_index, and its column scores become each generator's
        prescreen_score. No case verdict is stored and no round score moves."""
        _install(app, session_maker)
        await _enable(client)
        agents = [await _seed_agent(session_maker, rank) for rank in range(3)]
        _patch_panel(monkeypatch, agents)
        ids = await _seed_queue(session_maker)
        await _tick(session_maker)
        [batch] = await _batches(session_maker)

        work = await _lease(client)
        assert work is not None and work["kind"] == "generate"
        assert work["round_index"] == batch.prescreen_index >= PRESCREEN_INDEX_BASE
        assert work["cases_per_column"] == 16
        assert work["bench_version"] == batch.bench_version
        assert [c["submission_id"] for c in work["columns"]] == [
            str(ids["a"]),
            str(ids["b"]),
        ]
        assert await _lease(client) is None  # leased, and no other work
        generated = await client.post(
            "/api/v1/generator-scorer/round-results",
            headers=_SCORER,
            json=_generation(work),
        )
        assert generated.status_code == 200, generated.text
        assert generated.json() == {
            "round_index": batch.prescreen_index,
            "status": "running",
        }

        await _report_panel(session_maker, batch.prescreen_index)
        work = await _lease(client)
        assert work is not None and work["kind"] == "score"
        assert work["round_index"] == batch.prescreen_index
        assert work["params"]["target_cases"] == 16
        assert work["params"]["min_panel"] <= 3
        assert [c["counted"] for c in work["matrix"]["columns"]] == [True, True]
        assert len(work["matrix"]["agents"]) == 3
        scored = await client.post(
            "/api/v1/generator-scorer/round-score",
            headers=_SCORER,
            json=_matrix_score(work),
        )
        assert scored.status_code == 200, scored.text
        assert scored.json()["status"] == "scored"

        async with session_maker() as session:
            row = await get_prescreen(session, batch.prescreen_index)
            assert row is not None and row.status == "scored" and row.score
            for name, score in (("a", 0.1), ("b", 0.2)):
                sub = await session.get(GeneratorSubmission, ids[name])
                assert sub is not None
                assert sub.prescreen_index == batch.prescreen_index
                assert sub.prescreen_score == pytest.approx(score)
                assert sub.round_score is None
            verdicts = (await session.scalars(select(GeneratorRoundCaseVerdict))).all()
            assert verdicts == []

    async def test_a_failing_column_scores_zero_and_is_not_rejected(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install(app, session_maker)
        await _enable(client)
        agents = [await _seed_agent(session_maker, rank) for rank in range(3)]
        _patch_panel(monkeypatch, agents)
        ids = await _seed_queue(session_maker)
        await _tick(session_maker)
        work = await _lease(client)
        assert work is not None
        resp = await client.post(
            "/api/v1/generator-scorer/round-results",
            headers=_SCORER,
            json=_generation(work, failing=frozenset({"c1"})),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "running"
        async with session_maker() as session:
            failed = await session.get(GeneratorSubmission, ids["b"])
            assert failed is not None
            assert failed.status == "active"
            # Its zero waits for the batch to finish: a void batch keeps none.
            assert failed.prescreen_score is None

        [batch] = await _batches(session_maker)
        await _report_panel(session_maker, batch.prescreen_index)
        work = await _lease(client)
        assert work is not None and work["kind"] == "score"
        assert [c["column_id"] for c in work["matrix"]["columns"]] == ["c0"]
        scored = await client.post(
            "/api/v1/generator-scorer/round-score",
            headers=_SCORER,
            json=_matrix_score(work),
        )
        assert scored.status_code == 200, scored.text
        async with session_maker() as session:
            for name, score in (("a", 0.1), ("b", 0.0)):
                sub = await session.get(GeneratorSubmission, ids[name])
                assert sub is not None and sub.status == "active"
                assert sub.prescreen_index == batch.prescreen_index
                assert sub.prescreen_score == pytest.approx(score)

    async def test_an_all_failed_batch_scores_zero_without_a_panel_run(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """With every column failed the panel has nothing to run: the batch is
        scored at once, each generator scores zero, and the next batch takes
        other generators instead of failing the same ones again."""
        _install(app, session_maker)
        await _enable(client)
        agents = [await _seed_agent(session_maker, rank) for rank in range(3)]
        _patch_panel(monkeypatch, agents)
        ids = await _seed_queue(session_maker)
        await _tick(session_maker)
        work = await _lease(client)
        assert work is not None
        resp = await client.post(
            "/api/v1/generator-scorer/round-results",
            headers=_SCORER,
            json=_generation(work, failing=frozenset({"c0", "c1"})),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "scored"
        assert await _lease(client) is None
        [batch] = await _batches(session_maker)
        assert batch.status == "scored" and batch.void_reason is None
        async with session_maker() as session:
            for name in ("a", "b"):
                sub = await session.get(GeneratorSubmission, ids[name])
                assert sub is not None and sub.status == "active"
                assert sub.prescreen_index == batch.prescreen_index
                assert sub.prescreen_score == 0.0

        await _tick(session_maker)
        _, second = await _batches(session_maker)
        assert {c["submission_id"] for c in second.columns} == {
            str(ids["c"]),
            str(ids["a2"]),
        }

    async def test_a_void_batch_returns_every_generator_unscreened(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A running batch with a failed column that then expires keeps no
        score: both generators, the failed column's included, go back to the
        queue and open the next batch."""
        _install(app, session_maker)
        await _enable(client)
        agents = [await _seed_agent(session_maker, rank) for rank in range(3)]
        _patch_panel(monkeypatch, agents)
        ids = await _seed_queue(session_maker)
        now = datetime.now(UTC)
        await _tick(session_maker, now)
        work = await _lease(client)
        assert work is not None
        resp = await client.post(
            "/api/v1/generator-scorer/round-results",
            headers=_SCORER,
            json=_generation(work, failing=frozenset({"c1"})),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "running"

        await _tick(session_maker, now + PRESCREEN_TTL + timedelta(minutes=1))
        first, second = await _batches(session_maker)
        assert first.status == "void"
        assert {c["submission_id"] for c in second.columns} == {
            str(ids["a"]),
            str(ids["b"]),
        }
        async with session_maker() as session:
            for name in ("a", "b"):
                sub = await session.get(GeneratorSubmission, ids[name])
                assert sub is not None
                assert sub.prescreen_index == second.prescreen_index
                assert sub.prescreen_score is None

    async def test_round_work_comes_before_a_batch(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install(app, session_maker)
        await _enable(client, skip_unchanged_rounds=False)
        agents = [await _seed_agent(session_maker, rank) for rank in range(3)]
        _patch_panel(monkeypatch, agents)
        await _seed_queue(session_maker)
        await _tick(session_maker)  # a batch opens while the lane is idle
        await _advance(session_maker, 9)
        await _advance(session_maker, 12)  # round 3 opens beside it
        first = await _lease(client)
        assert first is not None and first["round_index"] == 3
        second = await _lease(client)
        assert second is not None and second["round_index"] >= PRESCREEN_INDEX_BASE

    async def test_scorer_faults_void_or_release_a_batch(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install(app, session_maker)
        await _enable(client)
        agents = [await _seed_agent(session_maker, rank) for rank in range(3)]
        _patch_panel(monkeypatch, agents)
        ids = await _seed_queue(session_maker)
        await _tick(session_maker)
        work = await _lease(client)
        assert work is not None

        def fault(kind: str, lease_id: str) -> dict[str, Any]:
            return {
                "work": "round",
                "lease_id": lease_id,
                "round_index": work["round_index"],
                "error_kind": kind,
                "detail": "docker daemon unavailable",
                "scorer_version": "challenge-scorer/7",
            }

        released = await client.post(
            "/api/v1/generator-scorer/fault",
            headers=_SCORER,
            json=fault("sandbox", work["lease_id"]),
        )
        assert released.status_code == 200, released.text
        assert released.json()["action"] == "released"
        again = await _lease(client)
        assert again is not None and again["round_index"] == work["round_index"]

        voided = await client.post(
            "/api/v1/generator-scorer/fault",
            headers=_SCORER,
            json=fault("unsupported_request", again["lease_id"]),
        )
        assert voided.status_code == 200, voided.text
        assert voided.json()["action"] == "voided"
        [batch] = await _batches(session_maker)
        assert batch.status == "void"
        assert (batch.void_reason or "").startswith("infra:")
        # Its generators go back to the queue unscreened.
        indexes = await _prescreen_indexes(session_maker, ids)
        assert indexes["a"] is None and indexes["b"] is None

    async def test_a_panel_id_in_another_text_form_still_counts_as_reported(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A batch compares panel agents to results as UUIDs, so a panel slot
        whose id is stored as upper-case text still closes the batch."""
        _install(app, session_maker)
        await _enable(client)
        agents = [await _seed_agent(session_maker, rank) for rank in range(3)]
        _patch_panel(monkeypatch, agents)
        await _seed_queue(session_maker)
        await _tick(session_maker)
        work = await _lease(client)
        assert work is not None
        resp = await client.post(
            "/api/v1/generator-scorer/round-results",
            headers=_SCORER,
            json=_generation(work),
        )
        assert resp.status_code == 200, resp.text
        [batch] = await _batches(session_maker)
        async with session_maker() as session, session.begin():
            row = await get_prescreen(session, batch.prescreen_index, for_update=True)
            assert row is not None
            row.panel = [
                {**slot, "agent_id": slot["agent_id"].upper()} for slot in row.panel
            ]
        await _report_panel(session_maker, batch.prescreen_index)
        work = await _lease(client)
        assert work is not None and work["kind"] == "score"
        assert len(work["matrix"]["agents"]) == 3

    async def test_a_lane_switched_off_leases_no_batch(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install(app, session_maker)
        await _enable(client)
        agents = [await _seed_agent(session_maker, rank) for rank in range(3)]
        _patch_panel(monkeypatch, agents)
        await _seed_queue(session_maker)
        await _tick(session_maker)
        await _apply_settings(client, validator_rounds_enabled=True)
        assert await _lease(client) is None


_JOB_URL = "/api/v1/validator/generator-round-job"


async def _job(client: httpx.AsyncClient) -> httpx.Response:
    return await client.post(
        _JOB_URL,
        headers={"X-Validator-Hotkey": _VALIDATOR.ss58_address},
        json=_signed_job(),
    )


class TestPrescreenPanel:
    async def test_validators_run_a_batch_panel_through_the_round_lane(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A batch's panel runs on round jobs whose round_index is the batch's:
        the dataset downloads through the private dataset route, results must
        cover the batch's cases, a failed lease returns the agent, and once
        every panel agent reported the batch goes to the scorer."""
        _install(app, session_maker)
        _patch_lane(monkeypatch)
        await _enable(client)
        agents = [await _seed_agent(session_maker, rank) for rank in range(3)]
        _patch_panel(monkeypatch, agents)
        ids = await _seed_queue(session_maker)
        await _tick(session_maker)
        work = await _lease(client)
        assert work is not None
        generated = _generation(work)
        resp = await client.post(
            "/api/v1/generator-scorer/round-results", headers=_SCORER, json=generated
        )
        assert resp.status_code == 200, resp.text
        [batch] = await _batches(session_maker)
        case_ids = [
            ref["case_id"]
            for refs in (batch.column_cases or {}).values()
            for ref in refs
        ]

        seen: set[str] = set()
        for index in range(3):
            resp = await _job(client)
            assert resp.status_code == 200, resp.text
            job = resp.json()
            assert job["round_index"] == batch.prescreen_index
            assert job["dataset_sha256"] == batch.dataset_sha256
            assert job["case_count"] == len(case_ids) == 32
            assert job["bench_version"] == batch.bench_version
            assert job["agent_id"] not in seen
            seen.add(job["agent_id"])
            score_url = (
                f"/api/v1/validator/agent/{job['agent_id']}/generator-round-score"
            )
            if index == 0:
                download = PrivateDatasetRequest(
                    validator_hotkey=_VALIDATOR.ss58_address,
                    dataset_sha256=job["dataset_sha256"],
                    deadline=datetime.fromisoformat(job["deadline"]),
                    nonce=uuid4(),
                    requested_at=datetime.now(UTC),
                    signature="0" * 128,
                )
                download.signature = _VALIDATOR.sign(
                    download.signing_message(UUID(job["agent_id"]))
                ).hex()
                body = await client.post(
                    f"/api/v1/validator/agent/{job['agent_id']}/private-dataset",
                    json=download.model_dump(mode="json"),
                )
                assert body.status_code == 200, body.text
                assert body.content == base64.b64decode(generated["dataset_base64"])
                partial = _signed_score(job, case_ids[:-1], 1.0)
                assert (await client.post(score_url, json=partial)).status_code == 422
                # A validator that cannot finish releases the lease, and the
                # agent is leased again.
                failed = await client.post(
                    f"/api/v1/validator/agent/{job['agent_id']}/generator-round-fail",
                    json=_signed_fail(job),
                )
                assert failed.status_code == 200, failed.text
                assert failed.json()["released"] is True
                resp = await _job(client)
                assert resp.status_code == 200, resp.text
                job = resp.json()
                assert job["agent_id"] in seen
            scored = await client.post(
                score_url, json=_signed_score(job, case_ids, 1.0 - index * 0.4)
            )
            assert scored.status_code == 200, scored.text
            assert scored.json()["accepted"] is True

        async with session_maker() as session:
            row = await get_prescreen(session, batch.prescreen_index)
            assert row is not None and row.status == "scoring"
            attempts = (
                await session.scalars(
                    select(GeneratorPrescreenAttempt).order_by(
                        GeneratorPrescreenAttempt.issued_at
                    )
                )
            ).all()
            assert [a.status for a in attempts] == [
                "failed",
                "completed",
                "completed",
                "completed",
            ]
        assert (await _job(client)).status_code == 204

        work = await _lease(client)
        assert work is not None and work["kind"] == "score"
        assert len(work["matrix"]["agents"]) == 3
        resp = await client.post(
            "/api/v1/generator-scorer/round-score",
            headers=_SCORER,
            json=_matrix_score(work),
        )
        assert resp.status_code == 200, resp.text
        async with session_maker() as session:
            for name in ("a", "b"):
                sub = await session.get(GeneratorSubmission, ids[name])
                assert sub is not None and sub.prescreen_score is not None

    async def test_no_batch_job_while_the_lane_is_off(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install(app, session_maker)
        _patch_lane(monkeypatch)
        await _enable(client)
        agents = [await _seed_agent(session_maker, rank) for rank in range(3)]
        _patch_panel(monkeypatch, agents)
        await _seed_queue(session_maker)
        await _tick(session_maker)
        work = await _lease(client)
        assert work is not None
        resp = await client.post(
            "/api/v1/generator-scorer/round-results",
            headers=_SCORER,
            json=_generation(work),
        )
        assert resp.status_code == 200, resp.text
        await _apply_settings(client, validator_rounds_enabled=True)
        assert (await _job(client)).status_code == 204

    async def test_a_void_batch_stops_its_panel_runs(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An expired batch expires its live panel tickets and closes their
        attempts, so a validator stops spending inference on it."""
        _install(app, session_maker)
        _patch_lane(monkeypatch)
        await _enable(client)
        agents = [await _seed_agent(session_maker, rank) for rank in range(3)]
        _patch_panel(monkeypatch, agents)
        await _seed_queue(session_maker)
        await _tick(session_maker)
        work = await _lease(client)
        assert work is not None
        resp = await client.post(
            "/api/v1/generator-scorer/round-results",
            headers=_SCORER,
            json=_generation(work),
        )
        assert resp.status_code == 200, resp.text
        job = (await _job(client)).json()

        await _tick(
            session_maker, datetime.now(UTC) + PRESCREEN_TTL + timedelta(minutes=1)
        )
        async with session_maker() as session:
            ticket = await session.get(
                ValidatorTicket,
                (UUID(job["agent_id"]), job["bench_version"], _VALIDATOR.ss58_address),
            )
            assert ticket is not None and ticket.status == TicketStatus.EXPIRED
            attempt = (await session.scalars(select(GeneratorPrescreenAttempt))).one()
            assert attempt.status == "failed"
            assert (attempt.detail or "").startswith("pre-screen void")
        late = await client.post(
            f"/api/v1/validator/agent/{job['agent_id']}/generator-round-score",
            json=_signed_score(job, ["c0/k0"], 1.0),
        )
        assert late.status_code == 409
