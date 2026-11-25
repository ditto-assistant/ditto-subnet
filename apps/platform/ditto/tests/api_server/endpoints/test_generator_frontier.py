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
from ditto.api_models.generator import (
    FRONTIER_ORACLE_MIN_BENCH_VERSION,
    FRONTIER_ORACLE_MODEL,
    GeneratorCompetitionSettings,
)
from ditto.api_models.generator_round_validator import (
    GeneratorRoundCaseResult,
    GeneratorRoundScoreRequest,
)
from ditto.api_models.generator_rounds import (
    frontier_audit_confirmation,
    frontier_settle_confirmation,
)
from ditto.api_models.private_dataset import PrivateDatasetRequest
from ditto.api_models.ticket_status import TicketStatus
from ditto.api_server.generator_frontier import (
    MAX_ORACLE_ATTEMPTS,
    MIN_ORACLE_FAILED_CALLS,
    ORACLE_MAX_FAILED_CHAT_FRACTION,
    STATE_AUDIT_PENDING,
    STATE_ORACLE_PENDING,
    STATE_SETTLED,
    FrontierRejected,
    advance_frontier_rounds,
    apply_frontier,
    frontier_audit_sample,
    frontier_hold_reason,
    oracle_inference_refusal,
    oracle_run_model,
    record_oracle_results,
    select_escalation,
    settle_frontier,
    settle_reason,
)
from ditto.api_server.generator_rounds import get_round, round_column_seed
from ditto.db.models import (
    GeneratorRound,
    GeneratorRoundAttempt,
    GeneratorRoundCaseVerdict,
    GeneratorSubmission,
    InferenceGrant,
    InferenceRequest,
    ValidatorTicket,
)
from ditto.db.queries.canonical_outage_voids import GrantOutage
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
    _signed_fail,
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
        attested = [
            (ref["case_id"], ref["fingerprint"])
            for ref in vector["frontier_cases"] or []
        ]
        got = apply_frontier(vector["base"], attested, vector["params"])
        # Bit-identical: the settled score is exactly ScoreCases' with the
        # attestations, so the post-pass never drifts from the contract.
        assert got == vector["expected"], vector["name"]


def test_an_attestation_never_applies_to_other_content_with_its_id() -> None:
    vector = next(
        v
        for v in json.loads((_CHALLENGE / "frontier_vectors.json").read_text())
        if v["name"] == "all_fail_all_attested"
    )
    stale = [
        (ref["case_id"], "other-content-" + ref["fingerprint"])
        for ref in vector["frontier_cases"]
    ]
    # The ids match but the content does not: nothing becomes frontier.
    assert apply_frontier(vector["base"], stale, vector["params"]) == vector["base"]


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
    # Every frontier policy field decides what a round measures, timing
    # included: a longer oracle timeout or audit grace can change which
    # attestations apply, so changing one never skips the next round.
    for field, value in (
        ("frontier_weight", 0.5),
        ("frontier_audit_cases", 5),
        ("frontier_audit_grace_hours", 48),
        ("frontier_oracle_timeout_hours", 24),
        ("frontier_oracle_model", FRONTIER_ORACLE_MODEL),
    ):
        changed = on.model_copy(update={field: value})
        assert changed.measurement_params() != on.measurement_params(), field
    # Unset, the oracle model leaves a policy's signature exactly as it was.
    assert "frontier_oracle_model" not in on.measurement_params()


def test_the_oracle_model_signs_only_rounds_it_takes_effect_on() -> None:
    # The model reaches a round's signature exactly where oracle_run_model
    # lets it change the run: bench FRONTIER_ORACLE_MIN_BENCH_VERSION and later.
    on = GeneratorCompetitionSettings(
        frontier_weight=1.0, frontier_oracle_agent_id=uuid4()
    )
    pinned = on.model_copy(update={"frontier_oracle_model": FRONTIER_ORACLE_MODEL})
    lane = {"oracle_model": FRONTIER_ORACLE_MODEL}
    below = FRONTIER_ORACLE_MIN_BENCH_VERSION - 1
    assert oracle_run_model(lane, bench_version=below) is None
    assert pinned.measurement_params(bench_version=below) == on.measurement_params(
        bench_version=below
    )
    for version in (FRONTIER_ORACLE_MIN_BENCH_VERSION, 13, 18):
        assert oracle_run_model(lane, bench_version=version) == FRONTIER_ORACLE_MODEL
        assert pinned.measurement_params(
            bench_version=version
        ) != on.measurement_params(bench_version=version), version


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


async def _book_oracle_chat(
    maker: Any,
    job: dict[str, Any],
    *,
    completed: int,
    failed: dict[str, int],
    started_at: datetime | None = None,
) -> None:
    """Record chat calls on the grant of ``job``'s oracle lease, as the
    inference proxy would while the oracle harness runs. The lane stubs
    ``ensure_inference_grant``, so the grant row is written here (once), on
    the lease's exact ticket key. ``started_at`` backdates the calls."""
    deadline = datetime.fromisoformat(job["deadline"])
    now = datetime.now(UTC)
    started_at = started_at or now
    async with maker() as session, session.begin():
        existing = await session.scalar(
            select(InferenceGrant).where(
                InferenceGrant.agent_id == UUID(job["agent_id"]),
                InferenceGrant.validator_hotkey == _VALIDATOR.ss58_address,
                InferenceGrant.ticket_deadline == deadline,
            )
        )
        grant = existing or InferenceGrant(
            grant_id=uuid4(),
            agent_id=UUID(job["agent_id"]),
            bench_version=int(job["bench_version"]),
            validator_hotkey=_VALIDATOR.ss58_address,
            slot_id=job["slot_id"],
            ticket_deadline=deadline,
            status="active",
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
            expires_at=deadline,
            created_at=now,
            updated_at=now,
        )
        if existing is None:
            session.add(grant)
            await session.flush()
        outcomes = [("completed", None)] * completed + [
            ("failed", code) for code, count in failed.items() for _ in range(count)
        ]
        for status, code in outcomes:
            session.add(
                InferenceRequest(
                    grant_id=grant.grant_id,
                    nonce=uuid4(),
                    generation=grant.generation,
                    status=status,
                    request_kind="chat",
                    model="openai/gpt-oss-20b",
                    reserved_tokens=16,
                    terminal_error_code=code,
                    started_at=started_at,
                    completed_at=started_at,
                )
            )


def test_oracle_inference_refusal_needs_a_failed_majority() -> None:
    def outage(chat: int, failed: int) -> GrantOutage:
        return GrantOutage(
            grant_id=uuid4(),
            chat_requests=chat,
            failed_requests=failed,
            outage_failed=0,
            failure_codes={"provider_context_length_exceeded": failed}
            if failed
            else {},
        )

    # Unknown or unused grants are not evidence of an outage.
    assert oracle_inference_refusal(None) is None
    assert oracle_inference_refusal(outage(0, 0)) is None
    # Production's healthiest and worst observed oracle runs (SN-496).
    assert oracle_inference_refusal(outage(192, 52)) is None
    assert oracle_inference_refusal(outage(725, 555)) is not None
    # The boundary is a failed share of at least ORACLE_MAX_FAILED_CHAT_FRACTION.
    assert ORACLE_MAX_FAILED_CHAT_FRACTION == 0.5
    assert oracle_inference_refusal(outage(100, 49)) is None
    refusal = oracle_inference_refusal(outage(100, 50))
    assert refusal is not None
    assert "50 of 100 chat calls" in refusal
    assert "provider_context_length_exceeded=50" in refusal

    # Below the evidence floor a run that also got answers is accepted: one or
    # two transient failures are not an outage.
    assert MIN_ORACLE_FAILED_CALLS == 5
    assert oracle_inference_refusal(outage(2, 1)) is None
    assert oracle_inference_refusal(outage(8, 4)) is None
    assert oracle_inference_refusal(outage(9, 5)) is not None
    # A run whose every call failed had no model at all: refused at any count.
    assert oracle_inference_refusal(outage(1, 1)) is not None
    assert oracle_inference_refusal(outage(3, 3)) is not None


def test_oracle_refusal_counts_every_failure_code_not_only_outage_class() -> None:
    # SN-496's blind runs had zero outage-class (402/429/503) failures: every
    # failed call was a context overflow, a provider 400 or a timeout. A gate
    # on outage-class failures alone would have accepted all ten.
    blind = GrantOutage(
        grant_id=uuid4(),
        chat_requests=725,
        failed_requests=555,
        outage_failed=0,
        failure_codes={"provider_context_length_exceeded": 555},
    )
    assert oracle_inference_refusal(blind) is not None


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
        self.chain = _install(app, self.maker)
        app.state.generator_round_loop = None
        agents = [await _seed_agent(self.maker, rank) for rank in range(5)]
        self.oracle = agents[3]
        # A stronger reference agent for the escalation tier; only on the
        # lane when a test sets frontier_escalation_agent_id to it.
        self.escalation = agents[4]
        if settings.pop("escalation", False):
            settings["frontier_escalation_agent_id"] = str(self.escalation)
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

    async def score_round(self, epochs: tuple[int, ...] = (3, 4)) -> int:
        for epoch in epochs:
            await _advance(self.maker, epoch)
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
        self,
        job: dict[str, Any],
        passed: set[str],
        *,
        status: int = 200,
        faulted: frozenset[str] = frozenset(),
        partial: frozenset[str] = frozenset(),
    ) -> httpx.Response:
        def score(case_id: str) -> float:
            if case_id in passed:
                return 1.0
            return 0.5 if case_id in partial else 0.0

        payload = _signed_score(job, job["oracle_case_ids"], 0.0)
        payload["results"] = [
            GeneratorRoundCaseResult(
                case_id=case_id,
                score=0.0 if case_id in faulted else score(case_id),
                fault=case_id in faulted,
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
        assert (queue["count"], queue["returned"], queue["has_more"]) == (1, 1, False)
        assert (queue["limit"], queue["offset"]) == (50, 0)
        past = (
            await client.get(_AUDITS_URL, headers=_ADMIN, params={"offset": 1})
        ).json()
        assert (past["count"], past["returned"], past["audits"]) == (1, 0, [])
        first = (
            await client.get(_AUDITS_URL, headers=_ADMIN, params={"limit": 1})
        ).json()
        assert first["has_more"] is False and first["returned"] == 1

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

    async def test_oracle_timeout_and_attempt_cap_hold_instead_of_settling(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Exhausting oracle attempts or timing out holds the round for
        operator action instead of auto-settling to zero with no audit stage."""
        lane = _Lane(client, session_maker)
        await lane.setup(app, monkeypatch, frontier_oracle_timeout_hours=2)
        round_index = await lane.score_round()
        now = datetime.now(UTC)
        async with session_maker() as session:
            row = await get_round(session, round_index)
            assert row is not None
            assert frontier_hold_reason(row, now=now) is None
            assert settle_reason(row, now=now) is None
            assert (
                frontier_hold_reason(row, now=now + timedelta(hours=2))
                == "oracle timed out"
            )
            assert settle_reason(row, now=now + timedelta(hours=2)) is None

            capped = dict(row.frontier or {})
            capped["oracle_attempts"] = MAX_ORACLE_ATTEMPTS
            row.frontier = capped
            assert frontier_hold_reason(row, now=now) == "oracle attempts exhausted"
            assert settle_reason(row, now=now) is None
            # While the final lease is still live, the round is neither held nor
            # settled under it.
            assert frontier_hold_reason(row, now=now, oracle_running=True) is None
            with pytest.raises(FrontierRejected, match="audit stage"):
                await settle_frontier(
                    session,
                    row,
                    params=GeneratorCompetitionSettings().score_params(),
                    reason="oracle attempts exhausted",
                    now=now,
                )
            await session.commit()

        # Automatic ticks record the hold once, leave the round unsettled, and
        # stop re-leasing oracle jobs until an operator intervenes.
        notes = await lane.tick(now=now)
        assert notes and "frontier held (oracle attempts exhausted)" in notes[0]
        assert await lane.tick(now=now + timedelta(minutes=5)) == []
        frontier = await lane.frontier(round_index)
        assert frontier["state"] == STATE_ORACLE_PENDING
        assert frontier["hold_reason"] == "oracle attempts exhausted"
        assert frontier["settled_at"] is None
        assert await _lease_job(client) is None

        # Explicit operator settlement still succeeds.
        resp = await client.post(
            f"{_ROUNDS_URL}/{round_index}/frontier/settle",
            headers=_ADMIN,
            json={
                "reason": "operator reviewed exhausted oracle round",
                "actor": "backroom:test",
                "confirmation": frontier_settle_confirmation(round_index),
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["state"] == STATE_SETTLED
        assert resp.json()["hold_reason"] is None

    async def test_zero_oracle_credit_holds_instead_of_settling_no_audit_required(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An accepted oracle run that passes zero cases across every column
        must not auto-settle vacuously as 'no audit required'."""
        lane = _Lane(client, session_maker)
        await lane.setup(app, monkeypatch, frontier_audit_cases=2)
        round_index = await lane.score_round()
        job = await lane.lease_oracle()
        await lane.report_oracle(job, set())

        frontier = await lane.frontier(round_index)
        assert frontier["state"] == STATE_AUDIT_PENDING
        assert frontier["attested"] == {}
        assert frontier["audits"] == []
        assert (
            frontier["hold_reason"] == "oracle credited zero cases across every column"
        )

        # Even well past audit grace, auto-settlement refuses to settle to 0.
        notes = await lane.tick()
        assert (
            notes
            and "frontier held (oracle credited zero cases across every column)"
            in notes[0]
        )
        assert await lane.tick(now=datetime.now(UTC) + timedelta(hours=48)) == []
        frontier = await lane.frontier(round_index)
        assert frontier["state"] == STATE_AUDIT_PENDING
        assert frontier["settled_at"] is None

        # Direct auto-settle guard refuses without operator_override=True.
        async with session_maker() as session:
            row = await get_round(session, round_index)
            assert row is not None
            with pytest.raises(FrontierRejected, match="zero cases across every"):
                await settle_frontier(
                    session,
                    row,
                    params=GeneratorCompetitionSettings().score_params(),
                    reason="no audit required",
                    now=datetime.now(UTC),
                )

    async def test_skipped_audit_stage_holds_and_refuses_auto_settle(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A round with attested cases and audit_cases > 0 whose audits are
        missing cannot auto-settle without the audit stage."""
        lane = _Lane(client, session_maker)
        await lane.setup(app, monkeypatch, frontier_audit_cases=2)
        round_index = await lane.score_round()
        job = await lane.lease_oracle()
        await lane.report_oracle(job, set(lane.candidates[:3]))

        async with session_maker() as session:
            row = await get_round(session, round_index)
            assert row is not None
            tampered = dict(row.frontier or {})
            tampered["audits"] = {}
            row.frontier = tampered
            await session.commit()

        frontier = await lane.frontier(round_index)
        assert frontier["state"] == STATE_AUDIT_PENDING
        assert frontier["hold_reason"] == "audit stage skipped"
        notes = await lane.tick()
        assert notes and "frontier held (audit stage skipped)" in notes[0]
        assert (await lane.frontier(round_index))["state"] == STATE_AUDIT_PENDING

        async with session_maker() as session:
            row = await get_round(session, round_index)
            assert row is not None
            assert settle_reason(row, now=datetime.now(UTC)) is None
            with pytest.raises(FrontierRejected, match="audit stage was skipped"):
                await settle_frontier(
                    session,
                    row,
                    params=GeneratorCompetitionSettings().score_params(),
                    reason="no audit required",
                    now=datetime.now(UTC),
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


class TestFrontierOracleLeases:
    async def test_an_all_faulted_oracle_run_is_refused_and_retried(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        lane = _Lane(client, session_maker)
        await lane.setup(app, monkeypatch)
        round_index = await lane.score_round()
        job = await lane.lease_oracle()
        faulted = _signed_score(job, job["oracle_case_ids"], 0.0, fault=True)
        resp = await client.post(
            f"/api/v1/validator/agent/{job['agent_id']}/generator-round-score",
            json=faulted,
        )
        # An infrastructure failure, not an answer: nothing is recorded.
        assert resp.status_code == 422, resp.text
        assert (await lane.frontier(round_index))["state"] == STATE_ORACLE_PENDING
        # The validator hands the lease back and another attempt runs.
        resp = await client.post(
            f"/api/v1/validator/agent/{job['agent_id']}/generator-round-fail",
            json=_signed_fail(job),
        )
        assert resp.status_code == 200, resp.text
        retry = await lane.lease_oracle()
        assert retry["oracle_case_ids"] == lane.candidates
        assert (await lane.frontier(round_index))["oracle_attempts"] == 2

    async def test_an_oracle_whose_model_calls_failed_upstream_is_refused(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # SN-496: the oracle's harness answered every /run, but its model
        # calls were refused upstream (context overflow), so it scored 0.0
        # on every case without faulting. Accepted, the round settled with
        # no attestations, as if every candidate had beaten the oracle.
        lane = _Lane(client, session_maker)
        await lane.setup(app, monkeypatch)
        round_index = await lane.score_round()
        job = await lane.lease_oracle()
        await _book_oracle_chat(
            session_maker,
            job,
            completed=1,
            failed={"provider_context_length_exceeded": 4, "upstream_http_400": 2},
        )
        resp = await lane.report_oracle(job, set(), status=422)
        detail = resp.json()["message"]
        assert "failed upstream on 6 of 7 chat calls" in detail
        assert "provider_context_length_exceeded=4" in detail
        # Nothing is recorded: the lane still waits on an oracle answer.
        frontier = await lane.frontier(round_index)
        assert frontier["state"] == STATE_ORACLE_PENDING
        # The validator hands the lease back and another attempt runs.
        resp = await client.post(
            f"/api/v1/validator/agent/{job['agent_id']}/generator-round-fail",
            json=_signed_fail(job),
        )
        assert resp.status_code == 200, resp.text
        retry = await lane.lease_oracle()
        assert retry["oracle_case_ids"] == lane.candidates
        assert (await lane.frontier(round_index))["oracle_attempts"] == 2
        # A healthy retry on its own grant attests as usual.
        await _book_oracle_chat(session_maker, retry, completed=4, failed={})
        await lane.report_oracle(retry, set(lane.candidates))
        assert (await lane.frontier(round_index))["state"] == STATE_AUDIT_PENDING

    async def test_only_the_current_attempts_calls_can_refuse_a_run(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A grant is minted per lease, but the refusal must not lean on that:
        # calls started before this attempt was issued (a reused grant's
        # earlier run) never count against the run reporting now.
        lane = _Lane(client, session_maker)
        await lane.setup(app, monkeypatch)
        round_index = await lane.score_round()
        job = await lane.lease_oracle()
        async with session_maker() as session:
            attempt = await session.scalar(
                select(GeneratorRoundAttempt).where(
                    GeneratorRoundAttempt.round_index == round_index,
                    GeneratorRoundAttempt.kind == "oracle",
                )
            )
            assert attempt is not None
            issued_at = attempt.issued_at
        await _book_oracle_chat(
            session_maker,
            job,
            completed=0,
            failed={"provider_context_length_exceeded": 20},
            started_at=issued_at - timedelta(minutes=10),
        )
        await _book_oracle_chat(session_maker, job, completed=4, failed={})
        # 20 of 24 calls on the grant failed, but none in this attempt.
        await lane.report_oracle(job, set(lane.candidates))
        assert (await lane.frontier(round_index))["state"] == STATE_AUDIT_PENDING

    async def test_an_oracle_with_a_few_failed_calls_still_reports(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Production's healthy oracle runs lose up to about a quarter of their
        # calls to timeouts and retries; that is an answer, not an outage.
        lane = _Lane(client, session_maker)
        await lane.setup(app, monkeypatch)
        round_index = await lane.score_round()
        job = await lane.lease_oracle()
        await _book_oracle_chat(
            session_maker, job, completed=3, failed={"provider_timeout": 1}
        )
        await lane.report_oracle(job, set())
        frontier = await lane.frontier(round_index)
        # Recorded: an oracle that genuinely failed every case attests none.
        assert frontier["state"] != STATE_ORACLE_PENDING

    async def test_a_running_oracle_lease_is_never_settled_under(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        lane = _Lane(client, session_maker)
        await lane.setup(app, monkeypatch, frontier_oracle_timeout_hours=1)
        round_index = await lane.score_round()
        job = await lane.lease_oracle()
        async with session_maker() as session, session.begin():
            row = await get_round(session, round_index, for_update=True)
            assert row is not None
            capped = dict(row.frontier or {})
            capped["oracle_attempts"] = MAX_ORACLE_ATTEMPTS
            row.frontier = capped
        # Past the cap and the timeout, the live lease still gets to deliver.
        later = datetime.now(UTC) + timedelta(hours=1, minutes=1)
        assert await lane.tick(now=later) == []
        await lane.report_oracle(job, set(lane.candidates))
        assert (await lane.frontier(round_index))["state"] == STATE_AUDIT_PENDING

    async def test_an_operator_settle_expires_the_running_oracle_lease(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        lane = _Lane(client, session_maker)
        await lane.setup(app, monkeypatch)
        round_index = await lane.score_round()
        job = await lane.lease_oracle()
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
        async with session_maker() as session:
            ticket = await session.scalar(
                select(ValidatorTicket).where(
                    ValidatorTicket.agent_id == lane.oracle,
                    ValidatorTicket.deadline == datetime.fromisoformat(job["deadline"]),
                )
            )
            assert ticket is not None and ticket.status == TicketStatus.EXPIRED
            attempt = await session.scalar(
                select(GeneratorRoundAttempt).where(
                    GeneratorRoundAttempt.round_index == round_index,
                    GeneratorRoundAttempt.kind == "oracle",
                )
            )
            assert attempt is not None and attempt.status == "failed"
        # The run's late result is refused.
        await lane.report_oracle(job, set(lane.candidates), status=409)

    async def test_an_expired_oracle_lease_is_closed_and_released(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        lane = _Lane(client, session_maker)
        await lane.setup(app, monkeypatch)
        round_index = await lane.score_round()
        job = await lane.lease_oracle()
        async with session_maker() as session, session.begin():
            ticket = await session.scalar(
                select(ValidatorTicket).where(
                    ValidatorTicket.agent_id == lane.oracle,
                    ValidatorTicket.deadline == datetime.fromisoformat(job["deadline"]),
                )
            )
            assert ticket is not None
            ticket.status = TicketStatus.EXPIRED
        await lane.lease_oracle()
        async with session_maker() as session:
            statuses = sorted(
                a.status
                for a in await session.scalars(
                    select(GeneratorRoundAttempt).where(
                        GeneratorRoundAttempt.round_index == round_index,
                        GeneratorRoundAttempt.kind == "oracle",
                    )
                )
            )
        assert statuses == ["failed", "leased"]


class TestFrontierOracleModel:
    """SN-496: the oracle answers on its own pinned model, not the panel's."""

    @staticmethod
    def _record_grants(
        monkeypatch: pytest.MonkeyPatch,
    ) -> list[str | None]:
        models: list[str | None] = []
        stub = lane_mod.ensure_inference_grant

        async def _grant(session: AsyncSession, **kwargs: Any) -> Any:
            models.append(kwargs.pop("model", None))
            return await stub(session, **kwargs)

        monkeypatch.setattr(lane_mod, "ensure_inference_grant", _grant)
        return models

    @staticmethod
    async def _frozen_model(maker: Any, round_index: int) -> str | None:
        """The oracle model the round's frontier lane froze when it scored."""
        async with maker() as session:
            row = await get_round(session, round_index)
            assert row is not None
            return (row.frontier or {}).get("oracle_model")

    async def test_the_oracle_lease_mints_its_grant_on_the_oracle_model(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        lane = _Lane(client, session_maker)
        await lane.setup(app, monkeypatch, frontier_oracle_model=FRONTIER_ORACLE_MODEL)
        round_index = await lane.score_round()
        models = self._record_grants(monkeypatch)
        # A validator whose scorer does not pin the oracle model gets no lease.
        monkeypatch.setattr(lane_mod, "oracle_model_capable", lambda *_a: False)
        assert await _lease_job(client) is None
        assert models == []
        # One that does is leased, on a grant minted on the oracle model.
        seen: list[str] = []

        def _capable(_heartbeat: Any, model: str) -> bool:
            seen.append(model)
            return True

        monkeypatch.setattr(lane_mod, "oracle_model_capable", _capable)
        job = await lane.lease_oracle()
        assert seen and set(seen) == {FRONTIER_ORACLE_MODEL}
        assert models == [FRONTIER_ORACLE_MODEL]
        assert (
            await self._frozen_model(session_maker, round_index)
            == FRONTIER_ORACLE_MODEL
        )
        # The round's frontier view reports it.
        frontier = await lane.frontier(round_index)
        assert frontier["oracle_model"] == FRONTIER_ORACLE_MODEL
        # The run reports exactly as before.
        await lane.report_oracle(job, set(lane.candidates))
        assert (await lane.frontier(round_index))["state"] == STATE_AUDIT_PENDING

    async def test_unset_the_oracle_runs_on_the_contract_model(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        lane = _Lane(client, session_maker)
        await lane.setup(app, monkeypatch)
        round_index = await lane.score_round()
        models = self._record_grants(monkeypatch)
        # No model to advertise: the capability is never consulted.
        monkeypatch.setattr(
            lane_mod,
            "oracle_model_capable",
            lambda *_a: pytest.fail("consulted without an oracle model"),
        )
        await lane.lease_oracle()
        assert models == [None]
        assert await self._frozen_model(session_maker, round_index) is None
        assert (await lane.frontier(round_index))["oracle_model"] is None

    async def test_a_settings_change_never_moves_an_open_lane(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The model is frozen into the lane when the round scores, like the
        # rest of its frontier policy.
        lane = _Lane(client, session_maker)
        await lane.setup(app, monkeypatch)
        round_index = await lane.score_round()
        await _apply_settings(
            client,
            validator_rounds_enabled=True,
            frontier_oracle_agent_id=str(lane.oracle),
            frontier_oracle_model=FRONTIER_ORACLE_MODEL,
        )
        models = self._record_grants(monkeypatch)
        await lane.lease_oracle()
        assert models == [None]
        assert await self._frozen_model(session_maker, round_index) is None
        assert (await lane.frontier(round_index))["oracle_model"] is None


def test_the_oracle_model_needs_a_lane_that_froze_it_and_bench_v9() -> None:
    lane = {"oracle_model": FRONTIER_ORACLE_MODEL}
    assert oracle_run_model(lane, bench_version=18) == FRONTIER_ORACLE_MODEL
    assert oracle_run_model(lane, bench_version=9) == FRONTIER_ORACLE_MODEL
    # The scorer pins no oracle route below v9.
    assert oracle_run_model(lane, bench_version=8) is None
    # A lane that predates the field, or froze it unset.
    assert oracle_run_model({}, bench_version=18) is None
    assert oracle_run_model({"oracle_model": None}, bench_version=18) is None
    assert oracle_run_model(None, bench_version=18) is None


class TestFrontierOracleWithARunningRound:
    async def test_an_unreadable_chain_head_still_leases_the_oracle(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        lane = _Lane(client, session_maker)
        await lane.setup(app, monkeypatch, skip_unchanged_rounds=False)
        scored = await lane.score_round()
        # The next round opens and runs, so a panel lease is possible too.
        await _advance(session_maker, 8)
        work = (
            await client.get("/api/v1/generator-scorer/round-work", headers=_SCORER)
        ).json()["work"]
        assert work["kind"] == "generate" and work["round_index"] == scored + 1
        resp = await client.post(
            "/api/v1/generator-scorer/round-results",
            headers=_SCORER,
            json=_generation(work),
        )
        assert resp.status_code == 200 and resp.json()["status"] == "running"
        # The head read only gates panel leases; the oracle never reads it.
        lane.chain.read_epoch_schedule.side_effect = RuntimeError("rpc down")
        job = await _lease_job(client)
        assert job is not None
        assert job["dataset_mode"] == "generator-frontier-oracle-v1"
        assert job["round_index"] == scored
        # Nothing else is claimable while the head stays unreadable.
        assert await _lease_job(client) is None


async def test_operator_settle_locks_oracle_tickets_before_the_round(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A result submission locks its ticket, then its round; an operator
    settle must take them in the same order or the two can deadlock."""
    import ditto.api_server.endpoints.admin_generator_rounds as admin_mod

    lane = _Lane(client, session_maker)
    await lane.setup(app, monkeypatch)
    round_index = await lane.score_round()
    job = await lane.lease_oracle()
    order: list[str] = []
    real_lock, real_get = admin_mod.lock_oracle_tickets, admin_mod.get_round

    async def lock(session: AsyncSession, index: int) -> Any:
        tickets = await real_lock(session, index)
        order.append(f"tickets:{len(tickets)}")
        return tickets

    async def get(session: AsyncSession, index: int, **kwargs: Any) -> Any:
        if kwargs.get("for_update"):
            order.append("round")
        return await real_get(session, index, **kwargs)

    monkeypatch.setattr(admin_mod, "lock_oracle_tickets", lock)
    monkeypatch.setattr(admin_mod, "get_round", get)
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
    assert order[:2] == ["tickets:1", "round"]
    await lane.report_oracle(job, set(lane.candidates), status=409)


async def test_an_oracle_lease_needs_the_rounds_column_size(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Like a panel run, an oracle run decodes the round's columns, so a
    validator whose scorer runs smaller columns is never leased one."""
    lane = _Lane(client, session_maker)
    await lane.setup(app, monkeypatch)
    await lane.score_round()  # 8-case columns
    monkeypatch.setattr(lane_mod, "_round_column_capacity", lambda *_a, **_k: 4)
    assert await _lease_job(client) is None
    monkeypatch.setattr(lane_mod, "_round_column_capacity", lambda *_a, **_k: 8)
    job = await lane.lease_oracle()
    assert job["case_count"] == len(lane.candidates)


def test_the_oracle_bound_is_the_largest_column_not_the_total() -> None:
    from types import SimpleNamespace

    from ditto.api_server.generator_frontier import oracle_column_cases

    def column(n: int) -> dict[str, Any]:
        return {"pack": {"cases": [{"case_id": f"k{i}"} for i in range(n)]}}

    # Two 70-case columns: 140 candidates at most, but each column is 70,
    # which an 80-case scorer runs one column at a time.
    row = SimpleNamespace(
        score={"columns": [column(70), column(70)]}, cases_per_column=70
    )
    assert oracle_column_cases(row) == 70  # type: ignore[arg-type]
    unscored = SimpleNamespace(score=None, cases_per_column=32)
    assert oracle_column_cases(unscored) == 32  # type: ignore[arg-type]


async def test_the_settlement_sweep_locks_an_expired_oracle_ticket_first(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A submission can lock its ticket just before the deadline and then wait
    on the round. Past the deadline the lease no longer counts as live, so the
    sweep holds the round and closes its stale attempt: it must lock the ticket
    before the round, as the submission does, or the two can deadlock."""
    import ditto.api_server.generator_frontier as frontier_mod
    import ditto.api_server.generator_rounds as rounds_mod

    lane = _Lane(client, session_maker)
    await lane.setup(app, monkeypatch, frontier_oracle_timeout_hours=1)
    round_index = await lane.score_round()
    job = await lane.lease_oracle()
    deadline = datetime.fromisoformat(job["deadline"])
    order: list[str] = []
    real_lock, real_expire, real_settle = (
        frontier_mod.lock_oracle_tickets,
        rounds_mod.expire_round_tickets,
        frontier_mod.settle_frontier,
    )

    async def lock(session: AsyncSession, index: int) -> Any:
        tickets = await real_lock(session, index)
        order.append(f"tickets:{index}:{len(tickets)}")
        return tickets

    async def expire(session: AsyncSession, row: Any, **kwargs: Any) -> Any:
        order.append(f"expire:{row.round_index}")
        return await real_expire(session, row, **kwargs)

    async def settle(session: AsyncSession, row: Any, **kwargs: Any) -> Any:
        order.append(f"settle:{row.round_index}")
        return await real_settle(session, row, **kwargs)

    monkeypatch.setattr(frontier_mod, "lock_oracle_tickets", lock)
    monkeypatch.setattr(rounds_mod, "expire_round_tickets", expire)
    monkeypatch.setattr(frontier_mod, "settle_frontier", settle)
    # Past the ticket deadline (the lease no longer counts as live) and the
    # oracle timeout, with the ticket still ISSUED.
    notes = await lane.tick(now=deadline + timedelta(hours=2))
    assert notes and "frontier held (oracle timed out)" in notes[0]
    assert order == [f"tickets:{round_index}:1", f"expire:{round_index}"]
    assert (await lane.frontier(round_index))["state"] == STATE_ORACLE_PENDING
    async with session_maker() as session:
        ticket = await session.scalar(
            select(ValidatorTicket).where(
                ValidatorTicket.agent_id == lane.oracle,
                ValidatorTicket.deadline == deadline,
            )
        )
        assert ticket is not None and ticket.status == TicketStatus.EXPIRED


async def test_an_older_incompatible_round_never_hides_younger_oracle_work(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rounds wait on the oracle oldest first, but a validator that cannot
    run the oldest (here: frozen at a bench version its scorer does not run)
    must still be leased a younger round it can run."""
    lane = _Lane(client, session_maker)
    await lane.setup(app, monkeypatch, skip_unchanged_rounds=False)
    older = await lane.score_round()
    # Round 2's panel runs while round 1 awaits its oracle: keep the oracle
    # lane off meanwhile so the panel loop leases only panel work.
    monkeypatch.setattr(lane_mod, "oracle_capable", lambda _heartbeat: False)
    younger = await lane.score_round(epochs=(8,))
    monkeypatch.setattr(lane_mod, "oracle_capable", lambda _heartbeat: True)
    assert younger == older + 1
    async with session_maker() as session, session.begin():
        row = await get_round(session, older, for_update=True)
        assert row is not None
        row.bench_version = 14

    def capacity(_heartbeat: Any, *, bench_version: int, **_: Any) -> int | None:
        return None if bench_version == 14 else 80

    monkeypatch.setattr(lane_mod, "_round_column_capacity", capacity)
    job = await lane.lease_oracle()
    assert job["round_index"] == younger
    # The older round stays pending for a validator that can run it.
    async with session_maker() as session:
        row = await get_round(session, older)
        assert row is not None and (row.frontier or {})["state"] == STATE_ORACLE_PENDING
        assert (row.frontier or {})["oracle_attempts"] == 0
    # Once the younger run reports (one live lease per agent and slot) and a
    # validator can run v14 rounds, the older round is leased too.
    await lane.report_oracle(job, set(job["oracle_case_ids"]))
    monkeypatch.setattr(lane_mod, "_round_column_capacity", lambda *_a, **_k: 80)
    assert (await lane.lease_oracle())["round_index"] == older


# --- escalation tier ----------------------------------------------------------


def test_escalation_off_keeps_the_unchanged_round_signature() -> None:
    on = GeneratorCompetitionSettings(frontier_oracle_agent_id=uuid4())
    assert not on.frontier_escalation_enabled
    with_agent = on.model_copy(update={"frontier_escalation_agent_id": uuid4()})
    assert with_agent.frontier_escalation_enabled
    # Escalation decides what a round measures only while it runs.
    assert with_agent.measurement_params() != on.measurement_params()
    frontier_off = with_agent.model_copy(update={"frontier_oracle_agent_id": None})
    assert not frontier_off.frontier_escalation_enabled
    assert frontier_off.measurement_params() == frontier_off.score_params()
    for field, value in (
        ("frontier_escalation_solve_threshold", 0.75),
        ("frontier_escalation_max_cases", 2),
        ("frontier_escalation_max_cases_per_column", 1),
        ("frontier_escalation_max_failed_fraction", 0.5),
    ):
        changed = with_agent.model_copy(update={field: value})
        assert changed.measurement_params() != with_agent.measurement_params(), field


def test_escalation_agent_is_neither_the_oracle_nor_a_reference_agent() -> None:
    oracle, reference = uuid4(), uuid4()
    with pytest.raises(ValueError, match="differ from frontier_oracle_agent_id"):
        GeneratorCompetitionSettings(
            frontier_oracle_agent_id=oracle, frontier_escalation_agent_id=oracle
        )
    with pytest.raises(ValueError, match="must not be a reference agent"):
        GeneratorCompetitionSettings(
            panel_size=5,
            frontier_oracle_agent_id=oracle,
            reference_agent_ids=[reference],
            frontier_escalation_agent_id=reference,
        )
    # Ships off.
    assert GeneratorCompetitionSettings().frontier_escalation_agent_id is None


class TestEscalationSelection:
    @staticmethod
    def _failed(columns: dict[str, int]) -> list[str]:
        return [f"{c}/k{i}" for c, n in columns.items() for i in range(n)]

    def test_caps_bound_the_cases_per_column_and_per_round(self) -> None:
        # Every column fails 10 of 70 cases (14%): all eligible, so only the
        # caps bound the spend however many cases a generator emits.
        failed = self._failed({f"c{i}": 10 for i in range(8)})
        sizes = {f"c{i}": 70 for i in range(8)}
        selected, skipped = select_escalation(
            round_seed="ab" * 32,
            failed=failed,
            column_sizes=sizes,
            max_cases=8,
            max_cases_per_column=4,
            max_failed_fraction=0.25,
        )
        assert skipped == {}
        assert len(selected) == 8
        per_column: dict[str, int] = {}
        for case_id in selected:
            per_column[case_id.split("/")[0]] = (
                per_column.get(case_id.split("/")[0], 0) + 1
            )
        assert max(per_column.values()) <= 4
        assert set(selected) <= set(failed)
        # One column alone never exceeds its own cap.
        alone, _ = select_escalation(
            round_seed="ab" * 32,
            failed=self._failed({"c0": 10}),
            column_sizes={"c0": 70},
            max_cases=8,
            max_cases_per_column=4,
            max_failed_fraction=0.25,
        )
        assert len(alone) == 4

    def test_a_flooded_column_is_never_escalated(self) -> None:
        # c1 is a generator that floods its pack with unsolvable cases: 40 of
        # 70 beat the oracle. It gets no escalation at all; c0 is unaffected.
        selected, skipped = select_escalation(
            round_seed="cd" * 32,
            failed=self._failed({"c0": 3, "c1": 40}),
            column_sizes={"c0": 70, "c1": 70},
            max_cases=8,
            max_cases_per_column=4,
            max_failed_fraction=0.25,
        )
        assert sorted(selected) == ["c0/k0", "c0/k1", "c0/k2"]
        assert list(skipped) == ["c1"]
        assert "40/70" in skipped["c1"]
        # At the boundary (exactly the fraction) a column is still eligible.
        at, skipped = select_escalation(
            round_seed="cd" * 32,
            failed=self._failed({"c1": 2}),
            column_sizes={"c1": 8},
            max_cases=8,
            max_cases_per_column=4,
            max_failed_fraction=0.25,
        )
        assert len(at) == 2 and skipped == {}

    def test_selection_is_seeded_and_order_independent(self) -> None:
        failed = self._failed({"c0": 12, "c1": 12})
        kwargs: dict[str, Any] = {
            "column_sizes": {"c0": 70, "c1": 70},
            "max_cases": 3,
            "max_cases_per_column": 2,
            "max_failed_fraction": 0.25,
        }
        one, _ = select_escalation(round_seed="11" * 32, failed=failed, **kwargs)
        again, _ = select_escalation(
            round_seed="11" * 32, failed=list(reversed(failed)), **kwargs
        )
        # Deterministic: the miner cannot reorder or resubmit its way in.
        assert one == again and len(one) == 3
        # The secret round seed, not the generator, decides which cases.
        picks = {
            tuple(select_escalation(round_seed=f"{i:064x}", failed=failed, **kwargs)[0])
            for i in range(8)
        }
        assert len(picks) > 1


class TestFrontierEscalation:
    """The panel and the oracle both fail a case; the escalation agent
    re-checks a capped sample of them."""

    @staticmethod
    async def _lease_escalation(lane: _Lane) -> dict[str, Any]:
        job = await _lease_job(lane.client)
        assert job is not None
        assert job["dataset_mode"] == "generator-frontier-oracle-v1"
        assert job["agent_id"] == str(lane.escalation)
        return job

    @staticmethod
    async def _statuses(
        maker: async_sessionmaker[AsyncSession], round_index: int, column: str
    ) -> dict[str, tuple[str, float]]:
        async with maker() as session:
            return {
                v.case_id: (v.status, float(v.credit))
                for v in await session.scalars(
                    select(GeneratorRoundCaseVerdict).where(
                        GeneratorRoundCaseVerdict.round_index == round_index,
                        GeneratorRoundCaseVerdict.column_id == column,
                    )
                )
            }

    async def test_a_stronger_pass_earns_frontier_credit_and_a_fail_stays_zero(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        lane = _Lane(client, session_maker)
        await lane.setup(app, monkeypatch, frontier_audit_cases=0, escalation=True)
        round_index = await lane.score_round()
        job = await lane.lease_oracle()
        # The oracle passes 6 of 8 and fails 2 (25%, at the flood guard).
        oracle_failed = lane.candidates[6:]
        await lane.report_oracle(job, set(lane.candidates[:6]))

        frontier = await lane.frontier(round_index)
        # The audits wait for the escalation run.
        assert frontier["state"] == STATE_ORACLE_PENDING
        assert frontier["oracle_deadline"] is None
        escalation = frontier["escalation"]
        assert escalation["outcome"] == "pending"
        assert escalation["oracle_failed"] == 2
        assert sorted(escalation["selected"]) == sorted(oracle_failed)
        assert escalation["agent_id"] == str(lane.escalation)
        assert escalation["deadline"] is not None
        # Oracle attestations are recorded but not applied yet.
        assert frontier["attested"] == {lane.miner_column: lane.candidates[:6]}

        esc_job = await self._lease_escalation(lane)
        assert esc_job["oracle_case_ids"] == escalation["selected"]
        assert esc_job["case_count"] == 2
        # One open lease at a time.
        assert await _lease_job(client) is None
        # The escalation agent reads the scored round's dataset, as the oracle.
        download = PrivateDatasetRequest(
            validator_hotkey=_VALIDATOR.ss58_address,
            dataset_sha256=esc_job["dataset_sha256"],
            deadline=datetime.fromisoformat(esc_job["deadline"]),
            nonce=uuid4(),
            requested_at=datetime.now(UTC),
            signature="0" * 128,
        )
        download.signature = _VALIDATOR.sign(
            download.signing_message(UUID(esc_job["agent_id"]))
        ).hex()
        body = await client.post(
            f"/api/v1/validator/agent/{esc_job['agent_id']}/private-dataset",
            json=download.model_dump(mode="json"),
        )
        assert body.status_code == 200, body.text

        hard_valid, broken = oracle_failed
        await lane.report_oracle(esc_job, {hard_valid})
        frontier = await lane.frontier(round_index)
        assert frontier["state"] == STATE_AUDIT_PENDING
        assert frontier["escalation"]["outcome"] == "reported"
        assert frontier["escalation"]["passed"] == [hard_valid]
        assert frontier["attested"] == {
            lane.miner_column: lane.candidates[:6] + [hard_valid]
        }

        notes = await lane.tick()
        assert notes and "no audit required" in notes[0]
        frontier = await lane.frontier(round_index)
        assert frontier["state"] == STATE_SETTLED
        # The rule: an escalated pass is an ordinary frontier case. 7 of 8
        # frontier at 1.5 credit (mean 0, champion failed), 1 unsolved:
        # 10.5 / (1.5 * 8) * (1 - 1/8)^2.
        want = 10.5 / (1.5 * 8) * (7 / 8) ** 2
        assert frontier["column_scores"][lane.miner_column] == pytest.approx(want)
        statuses = await self._statuses(session_maker, round_index, lane.miner_column)
        assert statuses[hard_valid] == ("frontier", pytest.approx(1.5))
        # Panel, oracle and escalation all failed it: still zero.
        assert statuses[broken] == ("unsolved", 0.0)

    async def test_an_escalation_that_fails_everything_adds_nothing(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        lane = _Lane(client, session_maker)
        await lane.setup(app, monkeypatch, frontier_audit_cases=0, escalation=True)
        round_index = await lane.score_round()
        job = await lane.lease_oracle()
        await lane.report_oracle(job, set(lane.candidates[:6]))
        esc_job = await self._lease_escalation(lane)
        # A half-right answer is not a full answer: the default threshold is
        # 1.0, so a 0.5 partial never attests at the escalation tier.
        await lane.report_oracle(esc_job, set(), partial=frozenset(lane.candidates[6:]))
        await lane.tick()
        frontier = await lane.frontier(round_index)
        assert frontier["state"] == STATE_SETTLED
        assert frontier["escalation"]["passed"] == []
        # Exactly the escalation-off score: 6 frontier, 2 unsolved.
        want = 9 / (1.5 * 8) * 0.75 * 0.75
        assert frontier["column_scores"][lane.miner_column] == pytest.approx(want)
        statuses = await self._statuses(session_maker, round_index, lane.miner_column)
        for case_id in lane.candidates[6:]:
            assert statuses[case_id] == ("unsolved", 0.0)

    async def test_escalated_passes_go_through_the_same_audit(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        lane = _Lane(client, session_maker)
        await lane.setup(app, monkeypatch, frontier_audit_cases=16, escalation=True)
        round_index = await lane.score_round()
        job = await lane.lease_oracle()
        await lane.report_oracle(job, set(lane.candidates[:6]))
        # No audit while the escalation runs.
        assert (await lane.frontier(round_index))["audits"] == []
        esc_job = await self._lease_escalation(lane)
        await lane.report_oracle(esc_job, set(esc_job["oracle_case_ids"]))
        (audit,) = (await lane.frontier(round_index))["audits"]
        # The sample covers every attested case, escalated ones included,
        # so an invalid escalated case voids the column like any other.
        assert set(audit["sample"]) == set(lane.candidates)
        url = f"{_ROUNDS_URL}/{round_index}/frontier/audits/{lane.miner_column}"
        resp = await client.post(
            url,
            headers=_ADMIN,
            json={
                "valid": False,
                "invalid_case_ids": [lane.candidates[7]],
                "reason": "the escalated case's gold is the superseded value",
                "actor": "backroom:test",
                "confirmation": frontier_audit_confirmation(
                    round_index, lane.miner_column, False
                ),
            },
        )
        assert resp.status_code == 200, resp.text
        await lane.tick()
        frontier = await lane.frontier(round_index)
        assert frontier["applied"] == {}
        assert frontier["column_scores"][lane.miner_column] == 0.0

    async def test_a_flooded_column_is_not_escalated(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        lane = _Lane(client, session_maker)
        await lane.setup(app, monkeypatch, frontier_audit_cases=0, escalation=True)
        round_index = await lane.score_round()
        job = await lane.lease_oracle()
        # The oracle fails every case: a column of deliberately unsolvable
        # cases buys no escalation and keeps the squared penalty.
        await lane.report_oracle(job, set())
        frontier = await lane.frontier(round_index)
        assert frontier["state"] == STATE_AUDIT_PENDING
        escalation = frontier["escalation"]
        assert escalation["outcome"] == "nothing selected"
        assert escalation["selected"] == []
        assert "8/8" in escalation["skipped_columns"][lane.miner_column]
        assert await _lease_job(client) is None
        await lane.tick()
        assert (await lane.frontier(round_index))["column_scores"][
            lane.miner_column
        ] == 0.0

    async def test_the_round_cap_and_faulted_cases_bound_the_selection(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        lane = _Lane(client, session_maker)
        await lane.setup(
            app,
            monkeypatch,
            frontier_audit_cases=0,
            escalation=True,
            frontier_escalation_max_cases=1,
            frontier_escalation_max_failed_fraction=0.5,
        )
        round_index = await lane.score_round()
        job = await lane.lease_oracle()
        # 4 passed, 1 faulted (the scorer's failure, never escalated), 3
        # failed: one case goes to escalation under the round cap.
        faulted = lane.candidates[4]
        await lane.report_oracle(
            job, set(lane.candidates[:4]), faulted=frozenset({faulted})
        )
        escalation = (await lane.frontier(round_index))["escalation"]
        assert escalation["oracle_failed"] == 3
        assert len(escalation["selected"]) == 1
        assert escalation["selected"][0] in lane.candidates[5:]
        esc_job = await self._lease_escalation(lane)
        assert esc_job["oracle_case_ids"] == escalation["selected"]

    async def test_an_escalation_that_never_reports_still_audits_the_oracle(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        lane = _Lane(client, session_maker)
        await lane.setup(
            app,
            monkeypatch,
            frontier_audit_cases=2,
            frontier_oracle_timeout_hours=1,
            escalation=True,
        )
        round_index = await lane.score_round()
        job = await lane.lease_oracle()
        await lane.report_oracle(job, set(lane.candidates[:6]))
        # Nothing settles (or opens) while the escalation still has time.
        assert await lane.tick() == []
        later = datetime.now(UTC) + timedelta(hours=1, minutes=1)
        notes = await lane.tick(now=later)
        assert notes == [f"round {round_index} frontier escalation timed out"]
        frontier = await lane.frontier(round_index)
        assert frontier["state"] == STATE_AUDIT_PENDING
        assert frontier["escalation"]["outcome"] == "escalation timed out"
        assert frontier["attested"] == {lane.miner_column: lane.candidates[:6]}
        (audit,) = frontier["audits"]
        assert set(audit["sample"]) <= set(lane.candidates[:6])
        # A timed-out escalation is never leased again.
        assert await _lease_job(client) is None

    async def test_an_operator_settle_during_escalation_keeps_oracle_credit(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        lane = _Lane(client, session_maker)
        await lane.setup(app, monkeypatch, frontier_audit_cases=2, escalation=True)
        round_index = await lane.score_round()
        job = await lane.lease_oracle()
        await lane.report_oracle(job, set(lane.candidates[:6]))
        await self._lease_escalation(lane)
        resp = await client.post(
            f"{_ROUNDS_URL}/{round_index}/frontier/settle",
            headers=_ADMIN,
            json={
                "reason": "escalation agent misconfigured for this round",
                "actor": "backroom:test",
                "confirmation": frontier_settle_confirmation(round_index),
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["applied"] == {lane.miner_column: lane.candidates[:6]}
        # The escalation was closed and the oracle's attestations drew their
        # audit sample before settling: no credit without one.
        frontier = await lane.frontier(round_index)
        assert frontier["escalation"]["outcome"].startswith("frontier settled:")
        (audit,) = frontier["audits"]
        assert len(audit["sample"]) == 2
        assert set(audit["sample"]) <= set(lane.candidates[:6])
        async with session_maker() as session:
            attempts = list(
                await session.scalars(
                    select(GeneratorRoundAttempt).where(
                        GeneratorRoundAttempt.round_index == round_index,
                        GeneratorRoundAttempt.kind == "oracle",
                    )
                )
            )
            # The escalation lease was expired, not left spending.
            assert sorted(a.status for a in attempts) == ["completed", "failed"]


def test_an_escalation_agent_seated_on_the_panel_is_never_leased() -> None:
    """A ledger agent picked as the escalation agent may also sit on the round
    panel; it must not re-roll its own failed panel attempt."""
    escalation = str(uuid4())
    cases = [
        {"case_id": f"c1/k{i}", "fingerprint": f"fp{i}", "status": "unsolved"}
        for i in range(8)
    ]
    row = GeneratorRound(
        round_index=1,
        seed="ab" * 32,
        panel=[{"agent_id": escalation}],
        score={
            "columns": [
                {
                    "column_id": "c1",
                    "pack": {"status": "scored", "duplicate": 0, "cases": cases},
                }
            ]
        },
        frontier={
            "state": STATE_ORACLE_PENDING,
            "phase": "oracle",
            "oracle_agent_id": str(uuid4()),
            "solve_threshold": 0.5,
            "audit_cases": 0,
            "candidates": [c["case_id"] for c in cases],
            "attested": {},
            "audits": {},
            "escalation_policy": {
                "agent_id": escalation,
                "solve_threshold": 1.0,
                "max_cases": 8,
                "max_cases_per_column": 4,
                "max_failed_fraction": 0.5,
            },
            "escalation": None,
        },
    )
    record_oracle_results(
        row,
        [
            GeneratorRoundCaseResult(case_id=c["case_id"], score=1.0 if i < 6 else 0.0)
            for i, c in enumerate(cases)
        ],
        validator_hotkey="5V",
        run_id="r",
        signature="s",
        now=datetime.now(UTC),
    )
    frontier = row.frontier or {}
    assert frontier["state"] == STATE_AUDIT_PENDING
    assert frontier["escalation"]["selected"] == []
    assert (
        frontier["escalation"]["outcome"] == "escalation agent sat on the round panel"
    )
    assert frontier["attested"] == {"c1": [f"c1/k{i}" for i in range(6)]}
    # The failed column is reported as skipped, with the reason.
    assert frontier["escalation"]["skipped_columns"] == {
        "c1": "escalation agent sat on the round panel"
    }
