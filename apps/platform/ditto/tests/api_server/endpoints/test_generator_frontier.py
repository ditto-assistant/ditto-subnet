"""Generator frontier cases: oracle attestations, sampled audits, settlement.

A case no two independent panel owners solve is ``unsolved``. The frontier
lane gives it a second witness: the frontier oracle (a subnet-run reference
agent answering from a bounded context) and a sampled operator audit. The
ports replay the Go contract's golden vectors; the lane is driven end to end
through the validator and admin endpoints against a real Postgres.
"""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import ditto.api_server.endpoints.validator_generator_round as lane_mod
from ditto.api_models.generator import GeneratorCompetitionSettings
from ditto.api_models.generator_round_validator import (
    GeneratorRoundCaseResult,
    GeneratorRoundScoreRequest,
)
from ditto.api_models.generator_rounds import (
    frontier_audit_confirmation,
    frontier_settle_confirmation,
)
from ditto.api_models.private_dataset import PrivateDatasetRequest
from ditto.api_server.generator_frontier import (
    MAX_ORACLE_ATTEMPTS,
    STATE_AUDIT_PENDING,
    STATE_ORACLE_PENDING,
    STATE_SETTLED,
    advance_frontier_rounds,
    apply_frontier,
    frontier_audit_sample,
    settle_reason,
)
from ditto.api_server.generator_rounds import get_round, round_column_seed
from ditto.db.models import (
    GeneratorRoundAttempt,
    GeneratorRoundCaseVerdict,
    GeneratorSubmission,
)
from ditto.tests.api_server.endpoints.test_generator_rounds import (
    _ADMIN,
    _JOB_URL,
    _REPO,
    _SCORER,
    _VALIDATOR,
    _advance,
    _apply_settings,
    _install,
    _lease_job,
    _patch_lane,
    _patch_panel,
    _qualify_all,
    _register_default,
    _report,
    _seed_agent,
    _seed_generator,
    _signed_job,
    _signed_score,
    round_capable_fleet,  # noqa: F401 - autouse: one round-capable validator
)

_CHALLENGE = _REPO / "research/dittobench-datagen/challenge/testdata"
_ROUNDS_URL = "/api/v1/admin/generator-competition/rounds"
_AUDITS_URL = "/api/v1/admin/generator-competition/frontier/audits"


# --- contract ports ---------------------------------------------------------


def test_apply_frontier_replays_the_go_vectors() -> None:
    vectors = json.loads((_CHALLENGE / "frontier_vectors.json").read_text())
    assert vectors
    for vector in vectors:
        got = apply_frontier(
            vector["base"], vector["frontier_case_ids"] or [], vector["params"]
        )
        # Bit-identical: the settled score is exactly ScoreCases' with the
        # attestations, so the post-pass never drifts from the contract.
        assert got == vector["expected"], vector["name"]


def test_audit_sample_replays_the_go_vectors() -> None:
    vectors = json.loads((_CHALLENGE / "round_vectors.json").read_text())
    audits = vectors["frontier_audits"]
    assert audits
    for vector in audits:
        assert frontier_audit_sample(
            vector["round_seed"], vector["column_id"], vector["case_ids"], vector["n"]
        ) == (vector["sample"] or [])


def test_frontier_off_keeps_the_unchanged_round_signature() -> None:
    off = GeneratorCompetitionSettings(frontier_weight=1.0)
    assert not off.frontier_enabled
    assert off.measurement_params() == off.score_params()
    on = off.model_copy(update={"frontier_oracle_agent_id": uuid4()})
    assert on.frontier_enabled
    assert on.measurement_params() != on.score_params()


# --- lane -------------------------------------------------------------------


def _pack(seed: str, cases: int) -> dict[str, Any]:
    """A small real-format pack: one user, two waves, a fact and a decoy per
    case, so an audit can read cases and memory from the stored dataset."""
    pairs0, pairs1, out = [], [], []
    for i in range(cases):
        pairs0.append(
            {
                "pair_id": f"old{i}",
                "session_id": "s0",
                "timestamp": f"2026-01-0{1 + i % 8}T09:00:00Z",
                "prompt": f"My locker {i} code is {1000 + i}.",
                "response": "Noted.",
            }
        )
        pairs1.append(
            {
                "pair_id": f"new{i}",
                "session_id": "s1",
                "timestamp": f"2026-02-0{1 + i % 8}T09:00:00Z",
                "prompt": f"They changed locker {i}; the code is now {5000 + i}.",
                "response": "Updated.",
            }
        )
        out.append(
            {
                "id": f"k{i}",
                "question_id": f"q{i}",
                "question_type": "knowledge-update",
                "question": f"What is the code for locker {i} now?",
                "expected_answer": str(5000 + i),
                "answer_kind": "number",
                "distractor_answers": [str(1000 + i)],
                "run_after_wave": 1,
                "evidence_pair_ids": [f"new{i}"],
            }
        )
    return {
        "contract": "dittobench-challenge-pack/v1",
        "grading_bench_version": 13,
        "miner_hotkey": _VALIDATOR.ss58_address,
        "generator": {"name": "t", "version": "1", "seed": seed},
        "memory_waves": [{"pairs": pairs0}, {"wave": 1, "pairs": pairs1}],
        "cases": out,
    }


def _generation(work: dict[str, Any]) -> dict[str, Any]:
    columns, dataset_columns = [], []
    for column in work["columns"]:
        cid = column["column_id"]
        seed = round_column_seed(work["seed"], cid)
        refs = [
            {
                "case_id": f"k{i}",
                "fingerprint": hashlib.sha256(f"{seed}:{i}".encode()).hexdigest(),
            }
            for i in range(work["cases_per_column"])
        ]
        pack = json.dumps(_pack(seed, len(refs)), separators=(",", ":"))
        columns.append(
            {
                "column_id": cid,
                "submission_id": column["submission_id"],
                "ok": True,
                "build": {"ok": True, "duration_ms": 5},
                "seed": {
                    "seed": seed,
                    "validation": {
                        "valid": True,
                        "case_count": len(refs),
                        "cases": refs,
                    },
                    "error": None,
                    "duration_ms": 7,
                },
                "error": None,
            }
        )
        dataset_columns.append(
            {
                "column_id": cid,
                "pack_sha256": hashlib.sha256(pack.encode()).hexdigest(),
                "pack": json.loads(pack),
            }
        )
    dataset = json.dumps(
        {
            "contract": "dittobench-generator-round/v1",
            "round_index": work["round_index"],
            "seed": work["seed"],
            "columns": sorted(dataset_columns, key=lambda c: c["column_id"]),
        },
        separators=(",", ":"),
    ).encode()
    return {
        "lease_id": work["lease_id"],
        "round_index": work["round_index"],
        "scorer_version": "challenge-scorer/7",
        "sandbox": "docker",
        "columns": columns,
        "dataset_sha256": hashlib.sha256(dataset).hexdigest(),
        "dataset_base64": base64.b64encode(dataset).decode(),
    }


def _unsolved_score(work: dict[str, Any], miner_columns: set[str]) -> dict[str, Any]:
    """Every agent fails every case of a miner column (unsolved, score 0);
    the public default column splits the panel as usual."""
    columns = []
    for column in work["matrix"]["columns"]:
        miner = column["column_id"] in miner_columns
        cases = [
            {
                "case_id": ref["case_id"],
                "fingerprint": ref["fingerprint"],
                "status": "unsolved" if miner else "discriminating",
                "panel_size": 3,
                "solvers": 0 if miner else 2,
                "solver_owners": 0 if miner else 2,
                "mean_score": 0.0 if miner else 0.5,
                "champion_failed": miner,
                "credit": 0.0 if miner else 0.5,
            }
            for ref in column["cases"]
        ]
        n = len(cases)
        columns.append(
            {
                "column_id": column["column_id"],
                "pack": {
                    "status": "scored",
                    "score": 0.0 if miner else 0.5 * n / (1.5 * max(n, 8)),
                    "raw_credit": 0.0 if miner else 0.5 * n,
                    "discriminating": 0 if miner else n,
                    "trivial": 0,
                    "unsolved": n if miner else 0,
                    "duplicate": 0,
                    "unsolved_rate": 1.0 if miner else 0.0,
                    "cases": cases,
                },
            }
        )
    agents = [
        {
            "agent_id": agent["agent_id"],
            "rank": rank + 1,
            "pairwise_score": 0.5,
            "wins": 0,
            "ties": 2,
            "losses": 0,
            "opponents": 2,
            "common_mean": 0.5,
            "common_cases": 8,
        }
        for rank, agent in enumerate(work["matrix"]["agents"])
    ]
    return {
        "lease_id": work["lease_id"],
        "round_index": work["round_index"],
        "scorer_version": "challenge-scorer/7",
        "score": {"columns": columns, "agents": agents},
    }


class _Lane:
    """One round driven to scored with an unsolved miner column."""

    def __init__(self, client: httpx.AsyncClient, maker: Any) -> None:
        self.client = client
        self.maker = maker
        self.oracle: UUID
        self.submission: UUID
        self.miner_column: str
        self.candidates: list[str]

    async def setup(
        self, app: FastAPI, monkeypatch: pytest.MonkeyPatch, **settings: Any
    ) -> None:
        _install(app, self.maker)
        app.state.generator_round_loop = None
        agents = [await _seed_agent(self.maker, rank) for rank in range(4)]
        self.oracle = agents[3]
        await _apply_settings(
            self.client,
            validator_rounds_enabled=True,
            frontier_oracle_agent_id=str(self.oracle),
            **settings,
        )
        _patch_panel(monkeypatch, agents[:3])
        _patch_lane(monkeypatch)
        monkeypatch.setattr(lane_mod, "oracle_capable", lambda _heartbeat: True)
        await _register_default(self.client)
        self.submission = await _seed_generator(
            self.maker, hotkey="5GenMinerA", owner="coldkey:ga", salt="a"
        )
        await _qualify_all(self.maker)

    async def score_round(self) -> int:
        await _advance(self.maker, 3)
        await _advance(self.maker, 4)
        work = (
            await self.client.get(
                "/api/v1/generator-scorer/round-work", headers=_SCORER
            )
        ).json()["work"]
        generated = _generation(work)
        resp = await self.client.post(
            "/api/v1/generator-scorer/round-results", headers=_SCORER, json=generated
        )
        assert resp.status_code == 200, resp.text
        case_ids = [
            f"{c['column_id']}/{ref['case_id']}"
            for c in generated["columns"]
            for ref in c["seed"]["validation"]["cases"]
        ]
        while (job := await _lease_job(self.client)) is not None:
            assert job["dataset_mode"] == "generator-round-v1"
            await _report(self.client, job, case_ids)
        work = (
            await self.client.get(
                "/api/v1/generator-scorer/round-work", headers=_SCORER
            )
        ).json()["work"]
        assert work["kind"] == "score"
        self.miner_column = next(
            c["column_id"]
            for c in work["matrix"]["columns"]
            if c["owner_key"] == "coldkey:ga"
        )
        resp = await self.client.post(
            "/api/v1/generator-scorer/round-score",
            headers=_SCORER,
            json=_unsolved_score(work, {self.miner_column}),
        )
        assert resp.status_code == 200, resp.text
        self.candidates = [c for c in case_ids if c.startswith(self.miner_column + "/")]
        return int(work["round_index"])

    async def lease_oracle(self) -> dict[str, Any]:
        job = await _lease_job(self.client)
        assert job is not None
        assert job["dataset_mode"] == "generator-frontier-oracle-v1"
        assert job["agent_id"] == str(self.oracle)
        return job

    async def report_oracle(
        self, job: dict[str, Any], passed: set[str], *, status: int = 200
    ) -> httpx.Response:
        payload = _signed_score(job, job["oracle_case_ids"], 0.0)
        payload["results"] = [
            GeneratorRoundCaseResult(
                case_id=case_id, score=1.0 if case_id in passed else 0.0
            ).model_dump(mode="json")
            for case_id in job["oracle_case_ids"]
        ]
        # Re-sign over the edited results.
        request = GeneratorRoundScoreRequest.model_validate(payload)
        request.signature = _VALIDATOR.sign(
            request.signing_message(UUID(job["agent_id"]))
        ).hex()
        resp = await self.client.post(
            f"/api/v1/validator/agent/{job['agent_id']}/generator-round-score",
            json=request.model_dump(mode="json"),
        )
        assert resp.status_code == status, resp.text
        return resp

    async def frontier(self, round_index: int) -> dict[str, Any]:
        resp = await self.client.get(
            f"{_ROUNDS_URL}/{round_index}/frontier", headers=_ADMIN
        )
        assert resp.status_code == 200, resp.text
        return resp.json()

    async def tick(self, *, now: datetime | None = None) -> list[str]:
        async with self.maker() as session, session.begin():

            async def params_for(_row: Any) -> dict[str, Any]:
                return {
                    "min_panel": 3,
                    "min_solver_owners": 2,
                    "solve_threshold": 0.5,
                    "champion_bonus": 0.5,
                    "target_cases": 8,
                }

            return await advance_frontier_rounds(
                session, params_for=params_for, now=now or datetime.now(UTC)
            )


class TestFrontierLane:
    async def test_attested_cases_score_after_a_passed_audit(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        lane = _Lane(client, session_maker)
        await lane.setup(app, monkeypatch, frontier_audit_cases=2)
        round_index = await lane.score_round()

        # Scored as before: the miner column pays 0 until the frontier settles.
        frontier = await lane.frontier(round_index)
        assert frontier["state"] == STATE_ORACLE_PENDING
        assert frontier["candidates"] == lane.candidates
        assert frontier["column_scores"][lane.miner_column] == 0.0

        job = await lane.lease_oracle()
        assert job["oracle_case_ids"] == lane.candidates
        assert job["case_count"] == len(lane.candidates)
        # One open oracle lease at a time.
        assert await _lease_job(client) is None

        # The oracle reads the scored round's dataset; nobody else does.
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

        # Results must name exactly the candidates.
        partial = _signed_score(job, job["oracle_case_ids"][:-1], 1.0)
        resp = await client.post(
            f"/api/v1/validator/agent/{job['agent_id']}/generator-round-score",
            json=partial,
        )
        assert resp.status_code == 422

        passed = set(lane.candidates[:6])
        await lane.report_oracle(job, passed)
        frontier = await lane.frontier(round_index)
        assert frontier["state"] == STATE_AUDIT_PENDING
        assert frontier["oracle"]["passed"] == 6
        assert frontier["attested"] == {lane.miner_column: lane.candidates[:6]}
        (audit,) = frontier["audits"]
        assert audit["status"] == "pending" and len(audit["sample"]) == 2
        assert set(audit["sample"]) <= passed
        assert audit["miner_hotkey"] == "5GenMinerA"

        queue = (await client.get(_AUDITS_URL, headers=_ADMIN)).json()
        assert [a["column_id"] for a in queue["audits"]] == [lane.miner_column]

        detail = await client.get(
            f"{_ROUNDS_URL}/{round_index}/frontier/audits/{lane.miner_column}",
            headers=_ADMIN,
            params={"q": "locker"},
        )
        assert detail.status_code == 200, detail.text
        case = detail.json()["cases"][0]
        assert case["question"].startswith("What is the code for locker")
        assert [p["pair_id"] for p in case["evidence"]] == [
            "new" + case["case_id"].rsplit("k", 1)[-1]
        ]
        assert case["visible_pairs"] == 16 and case["oracle_score"] == 1.0
        assert case["panel_mean_score"] == 0.0

        # Nothing settles while the audit is open.
        assert await lane.tick() == []

        # Ruling needs the exact confirmation.
        ruling = {
            "valid": True,
            "reason": "both sampled cases are fair supersession questions",
            "actor": "backroom:test",
            "confirmation": "RULE FRONTIER AUDIT",
        }
        url = f"{_ROUNDS_URL}/{round_index}/frontier/audits/{lane.miner_column}"
        assert (await client.post(url, headers=_ADMIN, json=ruling)).status_code == 409
        ruling["confirmation"] = frontier_audit_confirmation(
            round_index, lane.miner_column, True
        )
        resp = await client.post(url, headers=_ADMIN, json=ruling)
        assert resp.status_code == 200, resp.text
        assert resp.json()["audits"][0]["status"] == "passed"

        notes = await lane.tick()
        assert notes and "audits ruled" in notes[0]
        frontier = await lane.frontier(round_index)
        assert frontier["state"] == STATE_SETTLED
        assert frontier["applied"] == {lane.miner_column: lane.candidates[:6]}
        assert frontier["base_column_scores"][lane.miner_column] == 0.0
        # 6 of 8 cases frontier at 1.5 credit each, 2 still unsolved:
        # 9 / (1.5 * 8) * (1 - 2/8)^2.
        want = 9 / (1.5 * 8) * 0.75 * 0.75
        assert frontier["column_scores"][lane.miner_column] == pytest.approx(want)

        async with session_maker() as session:
            statuses = {
                v.case_id: v.status
                for v in await session.scalars(
                    select(GeneratorRoundCaseVerdict).where(
                        GeneratorRoundCaseVerdict.round_index == round_index,
                        GeneratorRoundCaseVerdict.column_id == lane.miner_column,
                    )
                )
            }
            assert {c for c, s in statuses.items() if s == "frontier"} == passed
            sub = await session.get(GeneratorSubmission, lane.submission)
            assert sub is not None and sub.round_score == pytest.approx(want)
            oracle_attempt = await session.scalar(
                select(GeneratorRoundAttempt).where(
                    GeneratorRoundAttempt.round_index == round_index,
                    GeneratorRoundAttempt.kind == "oracle",
                )
            )
            assert oracle_attempt is not None
            assert oracle_attempt.status == "completed"
        board = (await client.get("/api/v1/generator/board")).json()["entries"]
        assert [(e["miner_hotkey"], e["score"]) for e in board] == [
            ("5GenMinerA", pytest.approx(want))
        ]

    async def test_a_failed_audit_voids_the_columns_frontier_credit(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        lane = _Lane(client, session_maker)
        await lane.setup(app, monkeypatch, frontier_audit_cases=3)
        round_index = await lane.score_round()
        job = await lane.lease_oracle()
        await lane.report_oracle(job, set(lane.candidates))
        audit = (await lane.frontier(round_index))["audits"][0]
        url = f"{_ROUNDS_URL}/{round_index}/frontier/audits/{lane.miner_column}"
        # An invalid ruling must name sampled cases.
        bad = {
            "valid": False,
            "invalid_case_ids": [lane.candidates[0] + "-not-sampled"],
            "reason": "the gold answer is the superseded code",
            "actor": "backroom:test",
            "confirmation": frontier_audit_confirmation(
                round_index, lane.miner_column, False
            ),
        }
        assert (await client.post(url, headers=_ADMIN, json=bad)).status_code == 409
        bad["invalid_case_ids"] = [audit["sample"][0]]
        resp = await client.post(url, headers=_ADMIN, json=bad)
        assert resp.status_code == 200, resp.text
        # A ruled audit is final.
        assert (await client.post(url, headers=_ADMIN, json=bad)).status_code == 409

        await lane.tick()
        frontier = await lane.frontier(round_index)
        assert frontier["state"] == STATE_SETTLED
        assert frontier["applied"] == {}
        assert frontier["column_scores"][lane.miner_column] == 0.0
        board = (await client.get("/api/v1/generator/board")).json()["entries"]
        assert [e["score"] for e in board] == [0.0]

    async def test_an_unruled_audit_applies_after_its_grace(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        lane = _Lane(client, session_maker)
        await lane.setup(
            app, monkeypatch, frontier_audit_cases=2, frontier_audit_grace_hours=1
        )
        round_index = await lane.score_round()
        job = await lane.lease_oracle()
        await lane.report_oracle(job, set(lane.candidates))
        assert await lane.tick() == []
        notes = await lane.tick(now=datetime.now(UTC) + timedelta(hours=1, minutes=1))
        assert notes and "audit grace elapsed" in notes[0]
        frontier = await lane.frontier(round_index)
        assert frontier["state"] == STATE_SETTLED
        # Every case attested and none unsolved: full frontier credit.
        assert frontier["column_scores"][lane.miner_column] == pytest.approx(1.0)

    async def test_no_audit_applies_at_once_and_operator_can_settle(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        lane = _Lane(client, session_maker)
        await lane.setup(app, monkeypatch, frontier_audit_cases=0)
        round_index = await lane.score_round()
        # An operator settle while the oracle is pending applies nothing.
        resp = await client.post(
            f"{_ROUNDS_URL}/{round_index}/frontier/settle",
            headers=_ADMIN,
            json={
                "reason": "oracle agent misconfigured for this round",
                "actor": "backroom:test",
                "confirmation": frontier_settle_confirmation(round_index),
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["state"] == STATE_SETTLED
        assert resp.json()["applied"] == {}
        # A settled round leases no oracle run.
        assert await _lease_job(client) is None

    async def test_oracle_timeout_and_attempt_cap_settle_without_credit(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        lane = _Lane(client, session_maker)
        await lane.setup(app, monkeypatch, frontier_oracle_timeout_hours=2)
        round_index = await lane.score_round()
        async with session_maker() as session:
            row = await get_round(session, round_index)
            assert row is not None
            assert settle_reason(row, now=datetime.now(UTC)) is None
            assert (
                settle_reason(row, now=datetime.now(UTC) + timedelta(hours=2))
                == "oracle timed out"
            )
            capped = dict(row.frontier or {})
            capped["oracle_attempts"] = MAX_ORACLE_ATTEMPTS
            row.frontier = capped
            assert (
                settle_reason(row, now=datetime.now(UTC)) == "oracle attempts exhausted"
            )

    async def test_no_oracle_job_without_the_scorer_mode(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        lane = _Lane(client, session_maker)
        await lane.setup(app, monkeypatch)
        await lane.score_round()
        monkeypatch.setattr(lane_mod, "oracle_capable", lambda _heartbeat: False)
        resp = await client.post(
            _JOB_URL,
            headers={"X-Validator-Hotkey": _VALIDATOR.ss58_address},
            json=_signed_job(),
        )
        assert resp.status_code == 204

    async def test_frontier_off_opens_no_lane(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        lane = _Lane(client, session_maker)
        await lane.setup(app, monkeypatch, frontier_weight=0.0)
        round_index = await lane.score_round()
        resp = await client.get(f"{_ROUNDS_URL}/{round_index}/frontier", headers=_ADMIN)
        assert resp.status_code == 404
        assert await _lease_job(client) is None
