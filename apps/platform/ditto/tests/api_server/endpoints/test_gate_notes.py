"""Bench v13 gate evidence: storage, public aggregate, owner notes, dispute link.

Three disclosure levels for one fact (which case tripped which shadow gate):

- the validator posts the scorer's v13 report -- per-case ``catalog`` /
  ``claim_provenance`` / ``inference_cost`` records, twin markers in
  ``notes``, and the four ``details`` gate summaries -- and the platform
  projects it onto ``scores.gate_evidence``; ``NULL`` below the v13 floor, so
  v<=12 rows are byte-identical to before;
- the public per-score record carries the run-level aggregate only (posture,
  gate summaries, flagged-case count and share, per-finding counts);
- the owning hotkey reads the per-case notes with the ``note_id`` values a
  dispute cites; a foreign hotkey gets the same 404 as for an unknown agent.

The input is ``fixtures/score_report_v13.json``: a ``protocol.ScoreReport``
marshalled by the Go engine on the v13 scorer branches (catalog gate, claim
provenance, twins/cost) and unioned field-by-field, so every key and value
here is what the scorer really puts on the wire, not a Platform-side guess.

Seeding helpers are imported from ``test_validator`` / ``test_miner_logs``
rather than duplicated.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import bittensor
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.gate_evidence import (
    GATE_EVIDENCE_MIN_BENCH_VERSION,
    GATE_FINDINGS,
    GATE_NOTE_VOCABULARY,
    GATE_ZEROING_NOTES,
    TWIN_NOTE_MARKERS,
    StoredGateEvidence,
)
from ditto.api_models.validator import ScoreReport
from ditto.api_server.endpoints.public import screening_dispute_signing_message
from ditto.api_server.endpoints.validator import _score_details
from ditto.api_server.gate_evidence import (
    build_gate_evidence,
    gate_note_id,
    gate_note_ids_for,
    overall_posture,
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
_FIXTURE = (
    Path(__file__).resolve().parents[2] / "api_models/fixtures/score_report_v13.json"
)
# The shared finding list the Go scorer and this platform both pin.
_SHARED_FINDINGS = (
    Path(__file__).resolve().parents[6]
    / "services/dittobench-api/testdata/v13_gate_findings.json"
)
# The per-case keys a v<=12 row persisted before the v13 wire fields were
# mirrored onto ``CaseScore``. A v12 report must still store exactly these.
_LEGACY_CASE_KEYS = frozenset(
    {
        "case_id",
        "category",
        "kind",
        "score",
        "tool_score",
        "quality",
        "correct",
        "latency_ms",
        "called",
        "expected",
        "notes",
        "result_usage",
        "twin_group",
        "confidence",
        "observed",
        "injection",
    }
)


def _raw_report(*, bench_version: int = _V13) -> dict:
    raw = json.loads(_FIXTURE.read_text())
    raw["bench_version"] = bench_version
    raw["details"]["bench_version"] = bench_version
    return raw


def _report(*, bench_version: int = _V13) -> ScoreReport:
    return ScoreReport.model_validate(_raw_report(bench_version=bench_version))


# The pinned v13 projection of the captured report. If this moves, the stored
# contract moved.
_EXPECTED_STORED = {
    "contract_version": 1,
    "bench_version": _V13,
    "posture": "shadow",
    "catalog_gate": {
        "posture": "shadow",
        "tool_cases": 2,
        "attributed_cases": 2,
        "incomplete_capture_cases": 0,
        "lower_bound_cases": 0,
        "no_completion_cases": 0,
        "catalog_absent_cases": 1,
        "catalog_suppression_rate": 0.5,
        "safe_harbor_cases": 0,
        "restraint_without_offer": 1,
        "expected_tool_not_offered": 0,
        "swallowed_model_call": 1,
        "zeroed_cases": 0,
        "claim_uncorroborated_cases": 0,
        "attribution_coverage_bps": 10000,
    },
    "claim_provenance": {
        "posture": "shadow",
        "memory_cases": 3,
        "attributed_cases": 3,
        "applicable_cases": 2,
        "settled_cases": 2,
        "not_model_emitted_cases": 0,
        "answer_in_prompt_cases": 1,
        "no_model_completion_cases": 0,
        "unsettled_cases": 0,
        "zeroed_cases": 0,
        "attribution_coverage_bps": 10000,
    },
    "twin_post_pass": {
        "posture": "observe",
        "rule_requested": "concordant_zero",
        "rule": "concordant_zero",
        "honest_concordant_error_rate": None,
        "auto_fallback": False,
        "twin_groups": 1,
        "twin_groups_concordant": 1,
        "counterfactual_pairs": 1,
        "counterfactual_insensitive": 1,
        "cases_affected": 3,
        "cases_affected_share": 0.6,
        "applied": False,
    },
    "inference_cost": {
        "posture": "shadow",
        "applied": False,
        "floor_bps": 6000,
        "cases": 5,
        "attributed_cases": 5,
        "cases_below_full_factor": 1,
        "mean_factor_bps": 9733,
    },
    "catalog_suppression_rate": 0.5,
    "gate_counts": {
        "answer_in_prompt": 1,
        "catalog_absent": 1,
        "claim_not_applicable": 1,
        "counterfactual_insensitive": 2,
        "restraint_without_offer": 1,
        "swallowed_model_call": 1,
        "twin_concordant": 1,
    },
    "flagged_case_count": 4,
    "flagged_case_share": 0.666667,
    "cases": [
        {
            "case_index": 0,
            "case_id": "settings-9f3a-0001",
            "category": "set_theme",
            "kind": "tool",
            "score": 1.0,
            # Catalog findings in report order; the shadow cost factor
            # (8667 bps) rides beside them.
            "notes": [
                "catalog_absent",
                "restraint_without_offer",
                "swallowed_model_call",
            ],
            "relation": None,
            "cost_factor": 0.8667,
            "tools_offered": 0,
            "catalog_present": False,
        },
        {
            "case_index": 1,
            "case_id": "memory-9f3a-0002",
            "category": "temporal_reasoning",
            "kind": "memory",
            "score": 1.0,
            # Claim finding, then the twin marker from ``notes``.
            "notes": ["answer_in_prompt", "counterfactual_insensitive"],
            "relation": "base",
            "cost_factor": 1.0,
            "tools_offered": None,
            "catalog_present": None,
        },
        {
            "case_index": 2,
            "case_id": "memory-9f3a-0003",
            "category": "temporal_reasoning",
            "kind": "memory",
            "score": 1.0,
            "notes": ["counterfactual_insensitive"],
            "relation": "causal_counterfactual",
            "cost_factor": 1.0,
            "tools_offered": None,
            "catalog_present": None,
        },
        {
            "case_index": 4,
            "case_id": "memory-9f3a-0005",
            "category": "decision_recall",
            "kind": "memory",
            "score": 0.0,
            "notes": ["claim_not_applicable", "twin_concordant"],
            "relation": None,
            "cost_factor": 1.0,
            "tools_offered": None,
            "catalog_present": None,
        },
    ],
}


class TestVocabulary:
    def test_platform_vocabulary_is_the_shared_scorer_list(self) -> None:
        # One list for both sides of the wire: the Go scorer's finding
        # constants are pinned against the same file on the scorer branches.
        shared = json.loads(_SHARED_FINDINGS.read_text())["findings"]
        assert set(shared) == GATE_NOTE_VOCABULARY
        for note, spec in shared.items():
            finding = GATE_FINDINGS[note]
            assert finding.gate == spec["gate"], note
            assert finding.zeroing is spec["zeroing"], note
            assert finding.waived_by == frozenset(spec.get("waived_by", [])), note
        markers = json.loads(_SHARED_FINDINGS.read_text())["case_note_markers"]
        assert set(markers) == TWIN_NOTE_MARKERS

    def test_only_scorer_emitted_tokens_are_in_the_vocabulary(self) -> None:
        # Names that were rule / env values, never a finding, must not be here:
        # a note nothing ever appends would be a dead dispute target.
        for never_emitted in ("concordant_zero", "pair_product", "cost_factor_shadow"):
            assert never_emitted not in GATE_NOTE_VOCABULARY
        assert {"twin_concordant", "counterfactual_insensitive"} <= GATE_ZEROING_NOTES
        assert "expected_tool_not_offered" in GATE_ZEROING_NOTES


class TestBuildGateEvidence:
    def test_v13_projection_matches_the_pinned_vector(self) -> None:
        stored = build_gate_evidence(_report(), bench_version=_V13)
        assert stored == _EXPECTED_STORED
        # Round-trips through the stored model unchanged.
        assert StoredGateEvidence.model_validate(stored).model_dump(mode="json") == (
            stored
        )

    def test_below_the_floor_stores_nothing_and_details_are_untouched(self) -> None:
        # Same bytes on the wire, one version lower: no projection, and the
        # persisted details are exactly what they were before v13 existed --
        # including the per-case shape, which must NOT grow the newly mirrored
        # v10/v13 wire keys for a pre-v13 row.
        report = _report(bench_version=_PREVIOUS)
        assert build_gate_evidence(report, bench_version=_PREVIOUS) is None
        deadline = datetime(2030, 1, 1, tzinfo=UTC)
        details = _score_details(
            report, ticket_deadline=deadline, bench_version=_PREVIOUS
        )
        expected = dict(report.details or {})
        expected["ticket_deadline"] = deadline.isoformat(timespec="microseconds")
        expected["bench_version"] = _PREVIOUS
        expected["composite_stderr"] = report.composite_stderr
        expected["per_case"] = [
            {
                k: v
                for k, v in c.model_dump(mode="json").items()
                if k in _LEGACY_CASE_KEYS
            }
            for c in report.per_case
        ]
        assert json.dumps(details, sort_keys=True) == json.dumps(
            expected, sort_keys=True
        )
        for case in details["per_case"]:
            assert set(case) == _LEGACY_CASE_KEYS

    def test_v13_details_keep_the_whole_per_case_record(self) -> None:
        report = _report()
        details = _score_details(
            report, ticket_deadline=datetime(2030, 1, 1, tzinfo=UTC), bench_version=_V13
        )
        first = details["per_case"][0]
        assert first["catalog"]["findings"] == [
            "catalog_absent",
            "restraint_without_offer",
            "swallowed_model_call",
        ]
        assert first["inference_cost"]["class"] == "single_tool"
        assert first["tool_provenance"]["model_selected_not_executed"] == 1
        assert details["per_case"][1]["claim_provenance"]["answer_in_prompt"] is True
        assert details["per_case"][1]["relation"] == "base"
        # Go omits nil pointers; the stored record does too.
        assert "catalog" not in details["per_case"][1]

    def test_v13_report_without_gate_telemetry_stores_nothing(self) -> None:
        raw = _raw_report()
        for case in raw["per_case"]:
            for key in ("catalog", "claim_provenance", "inference_cost", "relation"):
                case.pop(key, None)
            case["notes"] = [
                n for n in case.get("notes", []) if n not in TWIN_NOTE_MARKERS
            ]
        for key in (
            "catalog_gate",
            "claim_provenance",
            "twin_post_pass",
            "inference_cost",
        ):
            raw["details"].pop(key)
        assert (
            build_gate_evidence(ScoreReport.model_validate(raw), bench_version=_V13)
            is None
        )

    def test_findings_alone_project_without_the_run_summaries(self) -> None:
        raw = _raw_report()
        for key in (
            "catalog_gate",
            "claim_provenance",
            "twin_post_pass",
            "inference_cost",
        ):
            raw["details"].pop(key)
        stored = build_gate_evidence(
            ScoreReport.model_validate(raw), bench_version=_V13
        )
        assert stored is not None
        # Findings with no summary can only have come from shadow gates.
        assert stored["posture"] == "shadow"
        assert (
            stored["catalog_gate"] is None
            and stored["catalog_suppression_rate"] is None
        )
        assert stored["gate_counts"] == _EXPECTED_STORED["gate_counts"]
        assert stored["cases"] == _EXPECTED_STORED["cases"]

    def test_one_malformed_summary_drops_only_itself(self) -> None:
        # Finding 4: a bad block must not erase the run verdict. Each summary
        # validates on its own.
        raw = _raw_report()
        raw["details"]["twin_post_pass"] = {"posture": "loud", "rule": "not a slug!"}
        raw["details"]["inference_cost"] = "nope"
        stored = build_gate_evidence(
            ScoreReport.model_validate(raw), bench_version=_V13
        )
        assert stored is not None
        assert stored["twin_post_pass"] is None
        assert stored["inference_cost"] is None
        assert stored["catalog_gate"] == _EXPECTED_STORED["catalog_gate"]
        assert stored["claim_provenance"] == _EXPECTED_STORED["claim_provenance"]
        assert stored["posture"] == "shadow"
        assert stored["cases"] == _EXPECTED_STORED["cases"]

    def test_enforce_on_any_gate_is_the_run_posture(self) -> None:
        raw = _raw_report()
        raw["details"]["claim_provenance"]["posture"] = "enforce"
        stored = build_gate_evidence(
            ScoreReport.model_validate(raw), bench_version=_V13
        )
        assert stored is not None and stored["posture"] == "enforce"
        assert overall_posture(["observe", None]) == "shadow"
        assert overall_posture(["off", None]) == "off"
        assert overall_posture([None]) is None

    def test_safe_harbor_waives_the_expected_tool_zero(self) -> None:
        raw = _raw_report()
        clean_tool = raw["per_case"][3]
        clean_tool["catalog"]["findings"] = [
            "expected_tool_not_offered",
            "semantic_preloading_safe_harbor",
        ]
        stored = build_gate_evidence(
            ScoreReport.model_validate(raw), bench_version=_V13
        )
        assert stored is not None
        # Counted, but not a would-be zero: the case is not flagged.
        assert stored["gate_counts"]["expected_tool_not_offered"] == 1
        assert stored["gate_counts"]["semantic_preloading_safe_harbor"] == 1
        assert [c["case_index"] for c in stored["cases"]] == [0, 1, 2, 4]
        # Without the harbor the same finding zeroes.
        clean_tool["catalog"]["findings"] = ["expected_tool_not_offered"]
        stored = build_gate_evidence(
            ScoreReport.model_validate(raw), bench_version=_V13
        )
        assert stored is not None
        assert [c["case_index"] for c in stored["cases"]] == [0, 1, 2, 3, 4]

    def test_unknown_findings_and_prose_never_survive(self) -> None:
        raw = _raw_report()
        raw["per_case"][0]["catalog"]["findings"].append("not-a-known-finding")
        raw["per_case"][1]["notes"].append('surfaced a wrong same-attribute value "42"')
        stored = build_gate_evidence(
            ScoreReport.model_validate(raw), bench_version=_V13
        )
        assert stored is not None
        published = {note for case in stored["cases"] for note in case["notes"]}
        assert published <= GATE_NOTE_VOCABULARY
        text = json.dumps(stored)
        for leaked in (
            "not-a-known-finding",
            '"42"',
            "recorded only",
            "twin post-pass",
        ):
            assert leaked not in text

    def test_public_projection_carries_aggregates_only(self) -> None:
        stored = build_gate_evidence(_report(), bench_version=_V13)
        public = public_gate_evidence(stored)
        assert public is not None
        assert public.flagged_case_count == 4
        assert public.flagged_case_share == pytest.approx(0.666667)
        assert public.catalog_suppression_rate == 0.5
        assert public.twin_post_pass is not None
        assert public.twin_post_pass.rule == "concordant_zero"
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
        assert first != gate_note_id(**{**kwargs, "gate": "twin_concordant"})  # type: ignore[arg-type]


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
    report = _report(bench_version=bench_version)
    stored = build_gate_evidence(report, bench_version=bench_version)
    async with maker() as s, s.begin():
        await upsert_score(
            s,
            agent_id=agent_id,
            validator_hotkey=_VALIDATOR,
            bench_version=bench_version,
            run_id="run_gate_1",
            seed=8675309,
            composite=0.7,
            tool_mean=0.75,
            memory_mean=0.6667,
            median_ms=1042,
            n=6,
            generated_at=_GENERATED_AT,
            signature="ab" * 64,
            details=_score_details(
                report,
                ticket_deadline=datetime(2030, 1, 1, tzinfo=UTC),
                bench_version=bench_version,
            ),
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
        assert evidence["flagged_case_count"] == 4
        assert evidence["flagged_case_share"] == pytest.approx(0.666667)
        assert evidence["catalog_suppression_rate"] == 0.5
        assert evidence["catalog_gate"]["posture"] == "shadow"
        assert evidence["twin_post_pass"]["posture"] == "observe"
        assert evidence["inference_cost"]["mean_factor_bps"] == 9733
        assert evidence["gate_counts"] == _EXPECTED_STORED["gate_counts"]
        # Aggregates only: no per-case notes, ids, or case ids on the wire.
        assert "cases" not in evidence
        for leaked in ('"note_id"', '"case_index"', "9f3a"):
            assert leaked not in resp.text
        # The public per-case view still drops the v13 gate findings (closed
        # vocabulary): they belong to the owner surface. The per-case
        # ``catalog`` / ``claim_provenance`` records never publish at all.
        public_cases = resp.json()["scores"][0]["case_results"]
        public_notes = [note for case in public_cases for note in (case["notes"] or [])]
        assert not (set(public_notes) & GATE_NOTE_VOCABULARY)
        for case in public_cases:
            assert not ({"catalog", "claim_provenance", "inference_cost"} & set(case))

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
        assert accepted and accepted[0]["gate_evidence"]["flagged_case_count"] == 4
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
        assert run["flagged_case_count"] == 4
        assert run["catalog_gate"]["catalog_suppression_rate"] == 0.5
        by_index = {case["case_index"]: case for case in run["cases"]}
        tool = by_index[0]
        assert tool["case_id"] == "settings-9f3a-0001"
        assert tool["tools_offered"] == 0 and tool["catalog_present"] is False
        assert tool["cost_factor"] == pytest.approx(0.8667)
        assert [n["gate"] for n in tool["notes"]] == [
            "catalog_absent",
            "restraint_without_offer",
            "swallowed_model_call",
        ]
        assert [n["zeroing"] for n in tool["notes"]] == [False, True, True]
        twin = by_index[1]
        assert twin["relation"] == "base"
        assert twin["case_id"] == "memory-9f3a-0002"
        note = next(n for n in twin["notes"] if n["gate"] == "answer_in_prompt")
        assert note["zeroing"] is True
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
    @pytest.mark.parametrize(
        "status", [AgentStatus.EVALUATING, AgentStatus.REJECTED, AgentStatus.LIVE]
    )
    async def test_owner_sees_case_ids_in_every_status(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        status: AgentStatus,
    ) -> None:
        # The seed of every accepted score is already on the public pipeline
        # record whatever the agent's status, so the seed-derived case id
        # reveals nothing to the owner they could not already derive -- and a
        # rejected owner can write their appeal against the case id, not a
        # bare index.
        miner = bittensor.Keypair.create_from_uri("//Alice")
        agent_id = await _seed_scored_agent(
            session_maker, miner_hotkey=miner.ss58_address, status=status
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
        assert run["flagged_case_count"] == 4
        assert [case["case_id"] for case in run["cases"]] == [
            "settings-9f3a-0001",
            "memory-9f3a-0002",
            "memory-9f3a-0003",
            "memory-9f3a-0005",
        ]

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
        for leaked in ("answer_in_prompt", "note_id", "9f3a", "causal_counterfactual"):
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
        assert payload["runs"][0]["flagged_case_count"] == 4


async def _reject_after_quarantine(
    maker: async_sessionmaker[AsyncSession], agent_id: UUID
) -> None:
    """Put a scored agent into the state the screening-dispute path accepts."""
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


_ADMIN_HEADERS = {
    "Authorization": "Bearer test-admin-token-at-least-32-characters",
    "X-Admin-Actor": "backroom:first-reviewer",
}


def _enable_admin(app: FastAPI) -> None:
    from dataclasses import replace

    app.state.config = replace(
        app.state.config,
        admin_api_token="test-admin-token-at-least-32-characters",
    )


class TestDisputeLink:
    @pytest.mark.asyncio
    async def test_rejected_dispute_cites_own_note_ids_and_operators_see_them(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _enable_admin(app)
        agent_id = await _seed_scored_agent(
            session_maker, miner_hotkey=_KEYPAIR.ss58_address
        )
        await _reject_after_quarantine(session_maker, agent_id)
        _install_db(app, session_maker)
        _install_chain(app)
        own = sorted(await _own_note_ids(session_maker, agent_id))
        assert len(own) == 8
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
        assert submitted.json()["dispute"]["kind"] == "screening"
        # The public projection stays private: no message, no ids.
        assert message not in submitted.text
        assert cited[0] not in submitted.text

        async with session_maker() as s:
            dispute = await s.scalar(select(ScreeningDispute))
            assert dispute is not None
            assert dispute.gate_note_ids == cited
            assert dispute.kind == "screening" and dispute.quarantine_id is not None

        listing = await client.get(
            "/api/v1/admin/screening-disputes", headers=_ADMIN_HEADERS
        )
        assert listing.status_code == 200, listing.text
        item = listing.json()["items"][0]
        assert item["gate_note_ids"] == cited
        assert item["kind"] == "screening"
        assert item["original_reason"] == "Initial rejection remains supported"

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
            assert dispute.kind == "screening"

    @pytest.mark.asyncio
    async def test_scored_agent_disputes_its_gate_notes_without_being_released(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        # The acceptance criterion: a shadow would-be zero is appealable
        # BEFORE enforce, by the agent that actually carries it -- which is
        # scored, not rejected -- and the resolution can never "release" a
        # scored agent back into evaluation.
        _enable_admin(app)
        agent_id = await _seed_scored_agent(
            session_maker, miner_hotkey=_KEYPAIR.ss58_address
        )
        _install_db(app, session_maker)
        _install_chain(app)
        own = sorted(await _own_note_ids(session_maker, agent_id))
        cited = own[:3]
        message = (
            "The twin_concordant marker fired on two decision twins that the "
            "seeded history answers identically; concordance here is correct."
        )
        signature = _sign(
            _KEYPAIR, screening_dispute_signing_message(agent_id, message)
        )

        # A scored agent citing nothing has no quarantine to appeal: 409.
        no_ids = await client.post(
            f"/api/v1/public/agent/{agent_id}/dispute",
            json={"message": message, "signature": signature},
        )
        assert no_ids.status_code == 409, no_ids.text
        # A foreign id is still 422, and nothing is written.
        foreign = await client.post(
            f"/api/v1/public/agent/{agent_id}/dispute",
            json={
                "message": message,
                "signature": signature,
                "gate_note_ids": ["0123456789abcdef"],
            },
        )
        assert foreign.status_code == 422, foreign.text
        async with session_maker() as s:
            assert (await s.scalar(select(ScreeningDispute))) is None

        submitted = await client.post(
            f"/api/v1/public/agent/{agent_id}/dispute",
            json={"message": message, "signature": signature, "gate_note_ids": cited},
        )
        assert submitted.status_code == 201, submitted.text
        assert submitted.json()["dispute"] == {
            "kind": "gate_notes",
            "status": "pending",
            "submitted_at": submitted.json()["dispute"]["submitted_at"],
            "resolved_at": None,
            "resolution": None,
        }
        async with session_maker() as s:
            dispute = await s.scalar(select(ScreeningDispute))
            assert dispute is not None
            assert dispute.kind == "gate_notes"
            assert dispute.quarantine_id is None
            assert dispute.gate_note_ids == cited
        # One dispute per submission, whichever kind.
        repeated = await client.post(
            f"/api/v1/public/agent/{agent_id}/dispute",
            json={"message": message, "signature": signature, "gate_note_ids": cited},
        )
        assert repeated.status_code == 409

        pipeline = await client.get(f"/api/v1/public/agent/{agent_id}/pipeline")
        assert pipeline.json()["dispute"]["kind"] == "gate_notes"
        assert message not in pipeline.text

        listing = await client.get(
            "/api/v1/admin/screening-disputes", headers=_ADMIN_HEADERS
        )
        assert listing.status_code == 200, listing.text
        assert listing.json()["count"] == 1
        item = listing.json()["items"][0]
        assert item["kind"] == "gate_notes"
        assert item["quarantine_id"] is None
        assert item["original_reason"] is None
        assert item["gate_note_ids"] == cited
        assert item["message"] == message

        # "release" on a gate-notes dispute records the operator's verdict on
        # the cited notes and leaves the scored agent exactly where it was.
        resolved = await client.post(
            f"/api/v1/admin/screening-disputes/{item['dispute_id']}/resolve",
            headers=_ADMIN_HEADERS,
            json={
                "resolution": "release",
                "reason": "Concordant decision twins are the seeded truth here.",
            },
        )
        assert resolved.status_code == 200, resolved.text
        assert resolved.json()["agent_status"] == "scored"
        assert resolved.json()["dispute"]["resolution"] == "release"
        assert resolved.json()["dispute"]["kind"] == "gate_notes"
        async with session_maker() as s:
            agent = await s.get(Agent, agent_id)
            assert agent is not None and agent.status == AgentStatus.SCORED
            score = await s.scalar(select(Score).where(Score.agent_id == agent_id))
            assert score is not None and score.gate_evidence is not None
        again = await client.post(
            f"/api/v1/admin/screening-disputes/{item['dispute_id']}/resolve",
            headers=_ADMIN_HEADERS,
            json={"resolution": "uphold", "reason": "already decided"},
        )
        assert again.status_code == 409

    @pytest.mark.asyncio
    async def test_upholding_a_gate_notes_dispute_changes_nothing_either(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _enable_admin(app)
        agent_id = await _seed_scored_agent(
            session_maker,
            miner_hotkey=_KEYPAIR.ss58_address,
            status=AgentStatus.EVALUATING,
        )
        _install_db(app, session_maker)
        _install_chain(app)
        cited = sorted(await _own_note_ids(session_maker, agent_id))[:1]
        message = "The catalog was suppressed by the validator's own tool_choice."
        signature = _sign(
            _KEYPAIR, screening_dispute_signing_message(agent_id, message)
        )
        submitted = await client.post(
            f"/api/v1/public/agent/{agent_id}/dispute",
            json={"message": message, "signature": signature, "gate_note_ids": cited},
        )
        assert submitted.status_code == 201, submitted.text
        listing = await client.get(
            "/api/v1/admin/screening-disputes", headers=_ADMIN_HEADERS
        )
        dispute_id = listing.json()["items"][0]["dispute_id"]
        upheld = await client.post(
            f"/api/v1/admin/screening-disputes/{dispute_id}/resolve",
            headers=_ADMIN_HEADERS,
            json={"resolution": "uphold", "reason": "The relay record is settled."},
        )
        assert upheld.status_code == 200, upheld.text
        assert upheld.json()["agent_status"] == "evaluating"
        assert upheld.json()["dispute"]["resolution"] == "uphold"
        async with session_maker() as s:
            agent = await s.get(Agent, agent_id)
            assert agent is not None and agent.status == AgentStatus.EVALUATING


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
        raw = _raw_report()
        resp = await client.post(
            f"/api/v1/validator/agent/{agent_id}/score",
            json=_score_payload(
                agent_id,
                run_id="run_gate_1",
                bench_version=_V13,
                composite=raw["composite"],
                n=raw["n"],
                per_case=raw["per_case"],
                details=raw["details"],
            ),
        )
        assert resp.status_code == 200, resp.text
        async with session_maker() as s:
            score = await s.scalar(select(Score).where(Score.agent_id == agent_id))
            assert score is not None
            assert score.gate_evidence is not None
            assert score.gate_evidence["gate_counts"] == _EXPECTED_STORED["gate_counts"]
            assert score.gate_evidence["cases"] == _EXPECTED_STORED["cases"]
            # The scorer's records ride in details whole: nothing v13 was
            # stripped at ingest (the drift the review found).
            assert score.details is not None
            assert score.details["catalog_gate"]["catalog_suppression_rate"] == 0.5
            assert score.details["twin_post_pass"]["rule"] == "concordant_zero"
            stored_first = score.details["per_case"][0]
            assert (
                stored_first["catalog"]["findings"]
                == raw["per_case"][0]["catalog"]["findings"]
            )
            assert (
                stored_first["inference_cost"] == raw["per_case"][0]["inference_cost"]
            )
            assert (
                score.details["per_case"][1]["claim_provenance"]
                == (raw["per_case"][1]["claim_provenance"])
            )

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
        raw = _raw_report(bench_version=_PREVIOUS)
        resp = await client.post(
            f"/api/v1/validator/agent/{agent_id}/score",
            json=_score_payload(
                agent_id,
                run_id="run_gate_1",
                bench_version=_PREVIOUS,
                composite=raw["composite"],
                n=raw["n"],
                per_case=copy.deepcopy(raw["per_case"]),
                details=copy.deepcopy(raw["details"]),
            ),
        )
        assert resp.status_code == 200, resp.text
        async with session_maker() as s:
            score = await s.scalar(select(Score).where(Score.agent_id == agent_id))
            assert score is not None
            assert score.gate_evidence is None
            assert score.details is not None
            # The advisory summaries still ride in details verbatim, exactly
            # as every other details key does; the per-case shape is the
            # pre-v13 one.
            assert score.details["catalog_gate"] == raw["details"]["catalog_gate"]
            for case in score.details["per_case"]:
                assert set(case) == _LEGACY_CASE_KEYS
