"""Bench v13 gate evidence: storage, public aggregate, owner notes, dispute link.

Three disclosure levels for one fact (which case tripped which shadow gate):

- the validator posts it (``details.gate_evidence`` + per-case ``notes``) and the
  platform projects it onto ``scores.gate_evidence`` -- ``NULL`` below the v13
  floor, so v<=12 rows are byte-identical to before;
- the public per-score record carries the run-level aggregate only (posture,
  composite with/without gates, gate-induced loss, per-gate counts);
- the owning hotkey reads the per-case notes with the ``note_id`` values a
  dispute cites; a foreign hotkey gets the same 404 as for an unknown agent.

Seeding helpers are imported from ``test_validator`` / ``test_miner_logs``
rather than duplicated.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import bittensor
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.gate_evidence import (
    GATE_EVIDENCE_MIN_BENCH_VERSION,
    GATE_NOTE_VOCABULARY,
    StoredGateEvidence,
)
from ditto.api_models.validator import ScoreReport
from ditto.api_server.endpoints.public import screening_dispute_signing_message
from ditto.api_server.endpoints.validator import _score_details
from ditto.api_server.gate_evidence import (
    build_gate_evidence,
    gate_note_id,
    gate_note_ids_for,
    public_gate_evidence,
)
from ditto.db.models import (
    Agent,
    AgentStatus,
    Score,
    ScreeningAttempt,
    ScreeningDispute,
    ScreeningQuarantine,
)
from ditto.db.queries.scores import upsert_score
from ditto.tests.api_server.endpoints.test_miner_logs import _login
from ditto.tests.api_server.endpoints.test_validator import (
    _KEYPAIR,
    _install_chain,
    _install_db,
    _score_payload,
    _seed_agent,
    _seed_ticket,
)

_V13 = GATE_EVIDENCE_MIN_BENCH_VERSION
_PREVIOUS = _V13 - 1
_VALIDATOR = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
_GENERATED_AT = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)


def _per_case() -> list[dict]:
    return [
        {
            "case_id": "settings-9f3a-0001",
            "category": "set_theme",
            "kind": "tool",
            "score": 1.0,
            "tool_score": 1.0,
            "latency_ms": 812,
            "called": [],
            "expected": [],
            # A v13 gate note beside an ordinary scorer note; only the former
            # is a gate note, and the value-bearing template is never one.
            "notes": [
                "restraint_without_offer",
                "answer incorporated the served tool result",
                'surfaced a wrong same-attribute value "42" (scored 0)',
            ],
        },
        {
            "case_id": "memory-9f3a-0002",
            "category": "temporal_reasoning",
            "kind": "memory",
            "score": 1.0,
            "tool_score": 0.0,
            "correct": True,
            "latency_ms": 1200,
            "called": [],
            "expected": [],
            "notes": ["deterministic number match"],
        },
        {
            "case_id": "memory-9f3a-0003",
            "category": "temporal_reasoning",
            "kind": "memory",
            "score": 1.0,
            "tool_score": 0.0,
            "correct": True,
            "latency_ms": 900,
            "called": [],
            "expected": [],
            "notes": [],
        },
    ]


def _gate_evidence_details() -> dict:
    return {
        "posture": "shadow",
        "composite_with_gates": 0.61,
        "composite_without_gates": 0.87,
        "catalog_suppression_rate": 0.02,
        "cases": [
            {
                "case_id": "memory-9f3a-0002",
                "notes": ["answer_in_prompt", "not-a-known-note"],
                "relation": "decision_twin",
                "relation_outcome": "concordant_zero",
                "cost_factor": 1.0,
                "tools_offered": 0,
                "score_with_gates": 0.0,
                "score_without_gates": 1.0,
            },
            # A case the per-case breakdown does not carry still projects.
            {
                "case_id": "tool-9f3a-0099",
                "notes": ["swallowed_model_call"],
                "tools_offered": 5,
            },
        ],
    }


def _report(*, bench_version: int, with_gates: bool = True) -> ScoreReport:
    details: dict = {"dataset_sha256": "cd" * 32, "bench_version": bench_version}
    if with_gates:
        details["gate_evidence"] = _gate_evidence_details()
    return ScoreReport.model_validate(
        {
            "run_id": "run_gate_1",
            "bench_version": bench_version,
            "seed": 8675309,
            "composite": 0.87,
            "tool_mean": 0.9,
            "memory_mean": 0.8,
            "median_ms": 812,
            "n": 3,
            "generated_at": _GENERATED_AT.isoformat(),
            "per_case": _per_case(),
            "details": details,
        }
    )


# The pinned v13 projection. If this moves, the stored contract moved.
_EXPECTED_STORED = {
    "contract_version": 1,
    "bench_version": _V13,
    "posture": "shadow",
    "composite_with_gates": 0.61,
    "composite_without_gates": 0.87,
    "gate_induced_loss": 0.26,
    "catalog_suppression_rate": 0.02,
    "gate_counts": {
        "answer_in_prompt": 1,
        "restraint_without_offer": 1,
        "swallowed_model_call": 1,
    },
    "relation_outcome_counts": {"concordant_zero": 1},
    "flagged_case_count": 3,
    "cases": [
        {
            "case_index": 0,
            "case_id": "settings-9f3a-0001",
            "category": "set_theme",
            "kind": "tool",
            "score": 1.0,
            "notes": ["restraint_without_offer"],
            "relation": None,
            "relation_outcome": None,
            "cost_factor": None,
            "tools_offered": None,
            "score_with_gates": None,
            "score_without_gates": None,
        },
        {
            "case_index": 1,
            "case_id": "memory-9f3a-0002",
            "category": "temporal_reasoning",
            "kind": "memory",
            "score": 1.0,
            "notes": ["answer_in_prompt"],
            "relation": "decision_twin",
            "relation_outcome": "concordant_zero",
            "cost_factor": 1.0,
            "tools_offered": 0,
            "score_with_gates": 0.0,
            "score_without_gates": 1.0,
        },
        {
            "case_index": None,
            "case_id": "tool-9f3a-0099",
            "category": None,
            "kind": None,
            "score": None,
            "notes": ["swallowed_model_call"],
            "relation": None,
            "relation_outcome": None,
            "cost_factor": None,
            "tools_offered": 5,
            "score_with_gates": None,
            "score_without_gates": None,
        },
    ],
}


class TestBuildGateEvidence:
    def test_v13_projection_matches_the_pinned_vector(self) -> None:
        stored = build_gate_evidence(_report(bench_version=_V13), bench_version=_V13)
        assert stored is not None
        assert stored["gate_induced_loss"] == pytest.approx(0.26)
        stored["gate_induced_loss"] = round(stored["gate_induced_loss"], 6)
        assert stored == _EXPECTED_STORED
        # Round-trips through the stored model unchanged.
        assert StoredGateEvidence.model_validate(stored).model_dump(mode="json") == (
            stored
        )

    def test_below_the_floor_stores_nothing_and_details_are_untouched(self) -> None:
        # Same bytes on the wire, one version lower: no projection, and the
        # persisted details are exactly what they were before v13 existed.
        report = _report(bench_version=_PREVIOUS)
        assert build_gate_evidence(report, bench_version=_PREVIOUS) is None
        deadline = datetime(2030, 1, 1, tzinfo=UTC)
        before = json.dumps(
            _score_details(report, ticket_deadline=deadline, bench_version=_PREVIOUS),
            sort_keys=True,
        )
        expected = dict(report.details or {})
        expected["ticket_deadline"] = deadline.isoformat(timespec="microseconds")
        expected["bench_version"] = _PREVIOUS
        expected["per_case"] = [c.model_dump(mode="json") for c in report.per_case]
        assert before == json.dumps(expected, sort_keys=True)

    def test_v13_report_without_gate_telemetry_stores_nothing(self) -> None:
        report = _report(bench_version=_V13, with_gates=False)
        # Strip the one gate note riding in per_case so nothing v13 remains.
        report.per_case[0].notes = ["answer incorporated the served tool result"]
        assert build_gate_evidence(report, bench_version=_V13) is None

    def test_notes_alone_project_without_the_run_level_object(self) -> None:
        report = _report(bench_version=_V13, with_gates=False)
        stored = build_gate_evidence(report, bench_version=_V13)
        assert stored is not None
        assert stored["posture"] is None
        assert stored["gate_induced_loss"] is None
        assert stored["gate_counts"] == {"restraint_without_offer": 1}
        assert [c["case_index"] for c in stored["cases"]] == [0]

    def test_malformed_run_level_object_is_dropped_not_raised(self) -> None:
        report = _report(bench_version=_V13)
        assert report.details is not None
        report.details["gate_evidence"] = {"posture": "loud", "cases": "nope"}
        stored = build_gate_evidence(report, bench_version=_V13)
        assert stored is not None
        assert stored["posture"] is None
        assert stored["gate_counts"] == {"restraint_without_offer": 1}

    def test_unknown_notes_never_survive(self) -> None:
        stored = build_gate_evidence(_report(bench_version=_V13), bench_version=_V13)
        assert stored is not None
        published = {note for case in stored["cases"] for note in case["notes"]}
        assert published <= GATE_NOTE_VOCABULARY
        assert "not-a-known-note" not in json.dumps(stored)
        assert '"42"' not in json.dumps(stored)

    def test_public_projection_carries_aggregates_only(self) -> None:
        stored = build_gate_evidence(_report(bench_version=_V13), bench_version=_V13)
        public = public_gate_evidence(stored)
        assert public is not None
        assert public.gate_induced_loss == pytest.approx(0.26)
        assert public.flagged_case_count == 3
        assert public.gate_counts["answer_in_prompt"] == 1
        assert "cases" not in public.model_dump()
        assert public_gate_evidence(None) is None
        assert public_gate_evidence({"bench_version": 3}) is None

    def test_note_id_is_stable_and_keyed_to_the_score(self) -> None:
        agent_id = uuid4()
        kwargs: dict = {
            "agent_id": agent_id,
            "bench_version": _V13,
            "validator_hotkey": _VALIDATOR,
            "run_id": "run_gate_1",
            "case_index": 1,
            "case_id": "memory-9f3a-0002",
            "gate": "answer_in_prompt",
        }
        first = gate_note_id(**kwargs)  # type: ignore[arg-type]
        assert first == gate_note_id(**kwargs)  # type: ignore[arg-type]
        assert len(first) == 16 and int(first, 16) >= 0
        assert first != gate_note_id(**{**kwargs, "agent_id": uuid4()})  # type: ignore[arg-type]
        assert first != gate_note_id(**{**kwargs, "gate": "slot_not_in_prose"})  # type: ignore[arg-type]


async def _seed_scored_agent(
    maker: async_sessionmaker[AsyncSession],
    *,
    miner_hotkey: str,
    status: AgentStatus = AgentStatus.SCORED,
    bench_version: int = _V13,
) -> UUID:
    agent_id = await _seed_agent(
        maker, status=status, miner_hotkey=miner_hotkey, dataset_version=bench_version
    )
    stored = build_gate_evidence(
        _report(bench_version=bench_version), bench_version=bench_version
    )
    async with maker() as s, s.begin():
        await upsert_score(
            s,
            agent_id=agent_id,
            validator_hotkey=_VALIDATOR,
            bench_version=bench_version,
            run_id="run_gate_1",
            seed=8675309,
            composite=0.87,
            tool_mean=0.9,
            memory_mean=0.8,
            median_ms=812,
            n=3,
            generated_at=_GENERATED_AT,
            signature="ab" * 64,
            details={"bench_version": bench_version, "per_case": _per_case()},
            gate_evidence=stored,
        )
    return agent_id


async def _own_note_ids(
    maker: async_sessionmaker[AsyncSession], agent_id: UUID
) -> frozenset[str]:
    async with maker() as s:
        scores = list(
            (await s.scalars(select(Score).where(Score.agent_id == agent_id))).all()
        )
    return gate_note_ids_for(agent_id=agent_id, scores=scores)


class TestPublicSurfaces:
    @pytest.mark.asyncio
    async def test_scores_detail_publishes_the_aggregate_never_the_notes(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        miner = bittensor.Keypair.create_from_uri("//Alice")
        agent_id = await _seed_scored_agent(
            session_maker, miner_hotkey=miner.ss58_address
        )
        _install_db(app, session_maker)

        resp = await client.get(f"/api/v1/public/agent/{agent_id}/scores")
        assert resp.status_code == 200, resp.text
        evidence = resp.json()["scores"][0]["gate_evidence"]
        assert evidence["posture"] == "shadow"
        assert evidence["bench_version"] == _V13
        assert evidence["composite_without_gates"] == pytest.approx(0.87)
        assert evidence["composite_with_gates"] == pytest.approx(0.61)
        assert evidence["gate_induced_loss"] == pytest.approx(0.26)
        assert evidence["gate_counts"] == {
            "answer_in_prompt": 1,
            "restraint_without_offer": 1,
            "swallowed_model_call": 1,
        }
        # Aggregates only: no per-case notes, ids, or case ids on the wire.
        assert "cases" not in evidence
        for leaked in ('"note_id"', '"case_index"', '"case_id"', "9f3a"):
            assert leaked not in resp.text
        # The public per-case view still drops the v13 gate notes (closed
        # vocabulary): they belong to the owner surface.
        public_notes = [
            note
            for case in resp.json()["scores"][0]["case_results"]
            for note in (case["notes"] or [])
        ]
        assert not (set(public_notes) & GATE_NOTE_VOCABULARY)

    @pytest.mark.asyncio
    async def test_pipeline_accepted_scores_carry_the_aggregate(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        miner = bittensor.Keypair.create_from_uri("//Alice")
        agent_id = await _seed_scored_agent(
            session_maker, miner_hotkey=miner.ss58_address
        )
        _install_db(app, session_maker)

        resp = await client.get(f"/api/v1/public/agent/{agent_id}/pipeline")
        assert resp.status_code == 200, resp.text
        accepted = resp.json()["provisional_scores"]
        assert accepted and accepted[0]["gate_evidence"]["gate_induced_loss"] == (
            pytest.approx(0.26)
        )
        assert '"note_id"' not in resp.text

    @pytest.mark.asyncio
    async def test_previous_version_rows_publish_null(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        miner = bittensor.Keypair.create_from_uri("//Alice")
        agent_id = await _seed_scored_agent(
            session_maker, miner_hotkey=miner.ss58_address, bench_version=_PREVIOUS
        )
        _install_db(app, session_maker)
        resp = await client.get(f"/api/v1/public/agent/{agent_id}/scores")
        assert resp.status_code == 200, resp.text
        assert resp.json()["scores"][0]["gate_evidence"] is None


class TestOwnerSurface:
    @pytest.mark.asyncio
    async def test_owner_reads_per_case_notes_with_note_ids(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        miner = bittensor.Keypair.create_from_uri("//Alice")
        agent_id = await _seed_scored_agent(
            session_maker, miner_hotkey=miner.ss58_address
        )
        _install_db(app, session_maker)
        _install_chain(app)
        token = await _login(client, keypair=miner)

        resp = await client.get(
            f"/api/v1/me/agents/{agent_id}/gate-notes",
            headers={"authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["agent_id"] == str(agent_id)
        assert body["miner_hotkey"] == miner.ss58_address
        (run,) = body["runs"]
        assert run["validator_hotkey"] == _VALIDATOR
        assert run["posture"] == "shadow"
        assert run["gate_induced_loss"] == pytest.approx(0.26)
        assert run["flagged_case_count"] == 3
        by_index = {case["case_index"]: case for case in run["cases"]}
        twin = by_index[1]
        assert twin["relation"] == "decision_twin"
        assert twin["relation_outcome"] == "concordant_zero"
        assert twin["cost_factor"] == 1.0
        assert twin["tools_offered"] == 0
        assert twin["score_without_gates"] == 1.0 and twin["score_with_gates"] == 0.0
        # A settled submission's seed is already public, so the owner sees the
        # seed-derived case id too.
        assert twin["case_id"] == "memory-9f3a-0002"
        (note,) = twin["notes"]
        assert note["gate"] == "answer_in_prompt"
        assert note["note_id"] == gate_note_id(
            agent_id=agent_id,
            bench_version=_V13,
            validator_hotkey=_VALIDATOR,
            run_id="run_gate_1",
            case_index=1,
            case_id="memory-9f3a-0002",
            gate="answer_in_prompt",
        )
        assert set(await _own_note_ids(session_maker, agent_id)) == {
            n["note_id"] for c in run["cases"] for n in c["notes"]
        }
        assert body["dispute_submit_url"].endswith("/dispute")

    @pytest.mark.asyncio
    async def test_provisional_submission_withholds_case_ids_from_owner(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        miner = bittensor.Keypair.create_from_uri("//Alice")
        agent_id = await _seed_scored_agent(
            session_maker,
            miner_hotkey=miner.ss58_address,
            status=AgentStatus.EVALUATING,
        )
        _install_db(app, session_maker)
        _install_chain(app)
        token = await _login(client, keypair=miner)

        resp = await client.get(
            f"/api/v1/me/agents/{agent_id}/gate-notes",
            headers={"authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text
        (run,) = resp.json()["runs"]
        # The verdict is visible before the run settles; the seed-derived id
        # is not, because the seed itself is not yet published.
        assert run["flagged_case_count"] == 3
        assert all(case["case_id"] is None for case in run["cases"])
        assert "9f3a" not in resp.text

    @pytest.mark.asyncio
    async def test_foreign_hotkey_never_sees_notes(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        owner = bittensor.Keypair.create_from_uri("//Alice")
        attacker = bittensor.Keypair.create_from_uri("//Bob")
        agent_id = await _seed_scored_agent(
            session_maker, miner_hotkey=owner.ss58_address
        )
        _install_db(app, session_maker)
        _install_chain(app)
        token = await _login(client, keypair=attacker)

        resp = await client.get(
            f"/api/v1/me/agents/{agent_id}/gate-notes",
            headers={"authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404
        for leaked in ("answer_in_prompt", "note_id", "9f3a", "decision_twin"):
            assert leaked not in resp.text
        # Same 404 as for an agent that does not exist at all.
        missing = await client.get(
            f"/api/v1/me/agents/{uuid4()}/gate-notes",
            headers={"authorization": f"Bearer {token}"},
        )
        assert missing.status_code == 404
        assert missing.json()["message"] == resp.json()["message"]

    @pytest.mark.asyncio
    async def test_anonymous_request_is_refused(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        miner = bittensor.Keypair.create_from_uri("//Alice")
        agent_id = await _seed_scored_agent(
            session_maker, miner_hotkey=miner.ss58_address
        )
        _install_db(app, session_maker)
        resp = await client.get(f"/api/v1/me/agents/{agent_id}/gate-notes")
        assert resp.status_code == 401
        assert "answer_in_prompt" not in resp.text

    @pytest.mark.asyncio
    async def test_owner_reads_notes_through_miner_mcp(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        miner = bittensor.Keypair.create_from_uri("//Alice")
        agent_id = await _seed_scored_agent(
            session_maker, miner_hotkey=miner.ss58_address
        )
        _install_db(app, session_maker)
        _install_chain(app)
        token = await _login(client, keypair=miner)

        listed = await client.post(
            "/mcp",
            headers={"authorization": f"Bearer {token}"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        assert "get_my_gate_notes" in [
            tool["name"] for tool in listed.json()["result"]["tools"]
        ]
        response = await client.post(
            "/mcp",
            headers={"authorization": f"Bearer {token}"},
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "get_my_gate_notes",
                    "arguments": {"agent_id": str(agent_id)},
                },
            },
        )
        assert response.status_code == 200, response.text
        result = response.json()["result"]
        assert result["isError"] is False
        payload = json.loads(result["content"][0]["text"])
        assert payload["runs"][0]["gate_induced_loss"] == pytest.approx(0.26)


async def _reject_after_quarantine(
    maker: async_sessionmaker[AsyncSession], agent_id: UUID
) -> None:
    """Put a scored agent into the one state the dispute path accepts."""
    now = datetime.now(UTC)
    attempt_id = uuid4()
    async with maker() as s, s.begin():
        agent = await s.get(Agent, agent_id)
        assert agent is not None
        agent.status = AgentStatus.REJECTED
        s.add(
            ScreeningAttempt(
                attempt_id=attempt_id,
                agent_id=agent_id,
                screener_hotkey="5Screener",
                policy_version=10,
                status="rejected",
                started_at=now - timedelta(minutes=1),
                deadline=now,
                finished_at=now,
            )
        )
        await s.flush()
        s.add(
            ScreeningQuarantine(
                quarantine_id=uuid4(),
                agent_id=agent_id,
                attempt_id=attempt_id,
                screener_hotkey="5Screener",
                policy_version=10,
                manifest_digest="56" * 32,
                reason_code="agentic-source-review-tripwire",
                status="resolved",
                created_at=now,
                resolved_at=now,
                resolved_by="backroom:first-reviewer",
                resolution="reject",
                resolution_reason="Initial rejection remains supported",
            )
        )


def _sign(keypair: bittensor.Keypair, payload: bytes) -> str:
    return keypair.sign(payload).hex()


class TestDisputeLink:
    @pytest.mark.asyncio
    async def test_dispute_cites_own_note_ids_and_operators_see_them(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        from dataclasses import replace

        app.state.config = replace(
            app.state.config,
            admin_api_token="test-admin-token-at-least-32-characters",
        )
        agent_id = await _seed_scored_agent(
            session_maker, miner_hotkey=_KEYPAIR.ss58_address
        )
        await _reject_after_quarantine(session_maker, agent_id)
        _install_db(app, session_maker)
        _install_chain(app)
        own = sorted(await _own_note_ids(session_maker, agent_id))
        assert len(own) == 3
        cited = own[:2]

        message = (
            "The answer_in_prompt note fired on a record the harness quoted "
            "verbatim from the seeded memory, which the records exemption covers."
        )
        signature = _sign(
            _KEYPAIR, screening_dispute_signing_message(agent_id, message)
        )
        # A foreign id is refused before anything is written, so the one
        # dispute is not spent on a malformed appeal.
        refused = await client.post(
            f"/api/v1/public/agent/{agent_id}/dispute",
            json={
                "message": message,
                "signature": signature,
                "gate_note_ids": [*cited, "0123456789abcdef"],
            },
        )
        assert refused.status_code == 422, refused.text
        async with session_maker() as s:
            assert (await s.scalar(select(ScreeningDispute))) is None

        submitted = await client.post(
            f"/api/v1/public/agent/{agent_id}/dispute",
            json={"message": message, "signature": signature, "gate_note_ids": cited},
        )
        assert submitted.status_code == 201, submitted.text
        assert submitted.json()["dispute"]["status"] == "pending"
        # The public projection stays private: no message, no ids.
        assert message not in submitted.text
        assert cited[0] not in submitted.text

        async with session_maker() as s:
            dispute = await s.scalar(select(ScreeningDispute))
            assert dispute is not None
            assert dispute.gate_note_ids == cited

        listing = await client.get(
            "/api/v1/admin/screening-disputes",
            headers={
                "Authorization": "Bearer test-admin-token-at-least-32-characters",
                "X-Admin-Actor": "backroom:first-reviewer",
            },
        )
        assert listing.status_code == 200, listing.text
        item = listing.json()["items"][0]
        assert item["gate_note_ids"] == cited
        # The cited ids resolve back to the owner view's notes.
        assert set(cited) <= set(own)

    @pytest.mark.asyncio
    async def test_dispute_without_note_ids_is_unchanged(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_scored_agent(
            session_maker, miner_hotkey=_KEYPAIR.ss58_address
        )
        await _reject_after_quarantine(session_maker, agent_id)
        _install_db(app, session_maker)
        _install_chain(app)
        message = "The screener misread a generic normalizer as benchmark logic."
        signature = _sign(
            _KEYPAIR, screening_dispute_signing_message(agent_id, message)
        )
        submitted = await client.post(
            f"/api/v1/public/agent/{agent_id}/dispute",
            json={"message": message, "signature": signature},
        )
        assert submitted.status_code == 201, submitted.text
        async with session_maker() as s:
            dispute = await s.scalar(select(ScreeningDispute))
            assert dispute is not None and dispute.gate_note_ids is None


class TestIngest:
    @pytest.mark.asyncio
    async def test_v13_score_report_stores_the_projection(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(
            session_maker, status=AgentStatus.EVALUATING, dataset_version=_V13
        )
        await _seed_ticket(session_maker, agent_id, bench_version=_V13)
        _install_db(app, session_maker)
        _install_chain(app)
        resp = await client.post(
            f"/api/v1/validator/agent/{agent_id}/score",
            json=_score_payload(
                agent_id,
                run_id="run_gate_1",
                bench_version=_V13,
                composite=0.87,
                n=3,
                per_case=_per_case(),
                details={
                    "dataset_sha256": "cd" * 32,
                    "gate_evidence": _gate_evidence_details(),
                },
            ),
        )
        assert resp.status_code == 200, resp.text
        async with session_maker() as s:
            score = await s.scalar(select(Score).where(Score.agent_id == agent_id))
            assert score is not None
            assert score.gate_evidence is not None
            assert score.gate_evidence["gate_counts"] == {
                "answer_in_prompt": 1,
                "restraint_without_offer": 1,
                "swallowed_model_call": 1,
            }
            # The advisory object still rides in details verbatim, exactly as
            # every other details key does.
            assert score.details is not None
            assert score.details["gate_evidence"]["posture"] == "shadow"

    @pytest.mark.asyncio
    async def test_previous_version_score_report_stores_null(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(
            session_maker, status=AgentStatus.EVALUATING, dataset_version=_PREVIOUS
        )
        await _seed_ticket(session_maker, agent_id, bench_version=_PREVIOUS)
        _install_db(app, session_maker)
        _install_chain(app)
        resp = await client.post(
            f"/api/v1/validator/agent/{agent_id}/score",
            json=_score_payload(
                agent_id,
                run_id="run_gate_1",
                bench_version=_PREVIOUS,
                composite=0.87,
                n=3,
                per_case=_per_case(),
                details={
                    "dataset_sha256": "cd" * 32,
                    "gate_evidence": _gate_evidence_details(),
                },
            ),
        )
        assert resp.status_code == 200, resp.text
        async with session_maker() as s:
            score = await s.scalar(select(Score).where(Score.agent_id == agent_id))
            assert score is not None
            assert score.gate_evidence is None
            assert score.details is not None
            assert score.details["gate_evidence"] == _gate_evidence_details()
