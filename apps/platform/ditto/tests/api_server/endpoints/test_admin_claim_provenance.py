"""Operator-only per-case v13 claim provenance (issue #1852).

The input is the same Go-marshalled ``score_report_v13.json`` the gate-notes
tests use, stored through the real ``_score_details`` / ``build_gate_evidence``
path, so the per-case record read here is exactly what production persists.
"""

from __future__ import annotations

import json
from dataclasses import replace
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.claim_provenance_cases import (
    NOT_PERSISTED_FIELDS,
    WITHHELD_SCORER_NOTE,
)
from ditto.api_models.gate_evidence import StoredGateEvidence
from ditto.api_server.gate_evidence import gate_note_ids_for
from ditto.db.models import Agent, Score
from ditto.tests.api_server.endpoints.test_gate_notes import (
    _PREVIOUS,
    _raw_report,
    _seed_scored_agent,
)
from ditto.tests.api_server.endpoints.test_validator import _install_db

_ADMIN_TOKEN = "test-admin-token-at-least-32-characters"
_HEADERS = {"Authorization": f"Bearer {_ADMIN_TOKEN}"}
_MINER = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"
_RUN = "run_gate_1"


def _install(app: FastAPI, maker: async_sessionmaker[AsyncSession]) -> None:
    app.state.config = replace(app.state.config, admin_api_token=_ADMIN_TOKEN)
    _install_db(app, maker)


async def _seeded(
    maker: async_sessionmaker[AsyncSession], *, bench_version: int | None = None
) -> tuple[UUID, str]:
    kwargs = {} if bench_version is None else {"bench_version": bench_version}
    agent_id = await _seed_scored_agent(maker, miner_hotkey=_MINER, **kwargs)
    async with maker() as s:
        agent = await s.get(Agent, agent_id)
        assert agent is not None
        return agent_id, agent.sha256


def _url(agent_id: UUID) -> str:
    return f"/api/v1/admin/agents/{agent_id}/claim-provenance"


async def _get(
    client: httpx.AsyncClient, agent_id: UUID, sha: str, **params: str | int
) -> httpx.Response:
    return await client.get(
        _url(agent_id),
        params={"artifact_sha256": sha, "run_id": _RUN, **params},
        headers=_HEADERS,
    )


class TestClaimProvenanceCases:
    async def test_default_view_is_the_stored_flagged_set(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id, sha = await _seeded(session_maker)
        _install(app, session_maker)

        resp = await _get(client, agent_id, sha)

        assert resp.status_code == 200, resp.text
        assert resp.headers["cache-control"] == "no-store"
        body = resp.json()
        async with session_maker() as s:
            score = (
                await s.scalars(select(Score).where(Score.agent_id == agent_id))
            ).one()
        stored = StoredGateEvidence.model_validate(score.gate_evidence)
        # Same rule as the public flagged_case_count, case for case.
        assert body["matched_cases"] == stored.flagged_case_count
        assert [c["case_index"] for c in body["cases"]] == [
            c.case_index for c in stored.cases
        ]
        assert body["total_cases"] == len(_raw_report()["per_case"])
        assert body["per_case_available"] is True
        assert body["truncated"] is False
        assert body["posture"] == stored.posture
        assert (
            body["claim_provenance"]
            == stored.model_dump(mode="json")["claim_provenance"]
        )
        assert body["artifact_sha256"] == sha
        assert body["run_id"] == _RUN
        assert body["not_persisted"] == list(NOT_PERSISTED_FIELDS)
        assert "not evidence either way" in body["not_persisted_reason"]

    async def test_note_ids_match_what_an_owner_dispute_cites(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id, sha = await _seeded(session_maker)
        _install(app, session_maker)

        body = (await _get(client, agent_id, sha)).json()

        async with session_maker() as s:
            scores = list(
                (await s.scalars(select(Score).where(Score.agent_id == agent_id))).all()
            )
        shown = {note["note_id"] for c in body["cases"] for note in c["gate_notes"]}
        assert shown
        assert shown == gate_note_ids_for(agent_id=agent_id, scores=scores)

    async def test_case_view_carries_the_persisted_claim_record_only(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id, sha = await _seeded(session_maker)
        _install(app, session_maker)

        resp = await _get(client, agent_id, sha, case_id="memory-9f3a-0002")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["matched_cases"] == 1
        (case,) = body["cases"]
        assert case["case_index"] == 1
        assert case["claim_provenance"] == {
            "posture": "shadow",
            "findings": ["answer_in_prompt"],
            "completions": 2,
            "unattributed_calls": 0,
            "tool_results": 0,
            "claim_tokens": 3,
            "complete": True,
            "model_emitted": True,
            "answer_in_prompt": True,
        }
        gates = {note["gate"]: note["zeroing"] for note in case["gate_notes"]}
        assert gates["answer_in_prompt"] is True
        assert any("claim provenance flagged" in n for n in case["scorer_notes"])
        # Never the answer key, the agent's tool calls, or response text.
        raw = json.dumps(body)
        for forbidden in ('"expected"', '"called"', '"final_text"', '"response"'):
            assert forbidden not in raw

    async def test_case_id_reaches_an_unflagged_case(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id, sha = await _seeded(session_maker)
        _install(app, session_maker)

        body = (
            await _get(client, agent_id, sha, case_id="web_search-9f3a-0004")
        ).json()

        (case,) = body["cases"]
        assert case["gate_notes"] == []
        assert case["claim_provenance"] is None

    async def test_catalog_record_is_projected_with_findings(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id, sha = await _seeded(session_maker)
        _install(app, session_maker)

        body = (await _get(client, agent_id, sha, case_id="settings-9f3a-0001")).json()

        (case,) = body["cases"]
        fixture_catalog = _raw_report()["per_case"][0]["catalog"]
        assert case["catalog"]["findings"] == fixture_catalog["findings"]
        assert case["catalog"]["tools_offered"] == len(
            fixture_catalog.get("tools_offered", [])
        )
        assert len(case["catalog"]["completions"]) == len(
            fixture_catalog.get("completions", [])
        )
        assert case["catalog"]["completions_truncated"] is False

    async def test_finding_filter_and_unknown_finding(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id, sha = await _seeded(session_maker)
        _install(app, session_maker)

        body = (await _get(client, agent_id, sha, finding="answer_in_prompt")).json()
        assert [c["case_id"] for c in body["cases"]] == ["memory-9f3a-0002"]
        # Informational findings are selectable too, not only zeroing ones.
        body = (
            await _get(client, agent_id, sha, finding="claim_not_applicable")
        ).json()
        assert [c["case_id"] for c in body["cases"]] == ["memory-9f3a-0005"]

        bad = await _get(client, agent_id, sha, finding="not_a_gate")
        assert bad.status_code == 422

    async def test_include_unflagged_and_limit_truncation(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id, sha = await _seeded(session_maker)
        _install(app, session_maker)
        total = len(_raw_report()["per_case"])

        body = (await _get(client, agent_id, sha, include_unflagged="true")).json()
        assert body["matched_cases"] == total
        assert [c["case_index"] for c in body["cases"]] == list(range(total))

        body = (
            await _get(client, agent_id, sha, include_unflagged="true", limit=2)
        ).json()
        assert body["matched_cases"] == total
        assert len(body["cases"]) == 2
        assert body["truncated"] is True

    async def test_every_key_is_exact(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id, sha = await _seeded(session_maker)
        _install(app, session_maker)

        other_sha = ("cd" * 32) if sha != "cd" * 32 else ("ef" * 32)
        assert (await _get(client, agent_id, other_sha)).status_code == 409
        assert (await _get(client, uuid4(), sha)).status_code == 404
        missing_run = await client.get(
            _url(agent_id),
            params={"artifact_sha256": sha, "run_id": "run_other"},
            headers=_HEADERS,
        )
        assert missing_run.status_code == 404
        missing_case = await _get(client, agent_id, sha, case_id="memory-none")
        assert missing_case.status_code == 404
        upper = await _get(client, agent_id, sha.upper())
        assert upper.status_code == 422

    async def test_pre_v13_run_is_refused(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id, sha = await _seeded(session_maker, bench_version=_PREVIOUS)
        _install(app, session_maker)

        resp = await _get(client, agent_id, sha)

        assert resp.status_code == 409
        assert "bench v13+" in resp.json()["message"]

    async def test_malformed_and_missing_breakdowns_are_explicit(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id, sha = await _seeded(session_maker)
        _install(app, session_maker)
        async with session_maker() as s, s.begin():
            score = (
                await s.scalars(select(Score).where(Score.agent_id == agent_id))
            ).one()
            details = dict(score.details or {})
            per_case = list(details["per_case"])
            per_case[3] = {"not": "a case"}
            details["per_case"] = per_case
            await s.execute(
                update(Score).where(Score.agent_id == agent_id).values(details=details)
            )

        body = (await _get(client, agent_id, sha, include_unflagged="true")).json()
        assert body["malformed_cases"] == 1
        assert body["total_cases"] == len(per_case)
        assert 3 not in [c["case_index"] for c in body["cases"]]

        async with session_maker() as s, s.begin():
            details.pop("per_case")
            await s.execute(
                update(Score).where(Score.agent_id == agent_id).values(details=details)
            )
        body = (await _get(client, agent_id, sha)).json()
        assert body["per_case_available"] is False
        assert body["cases"] == []
        assert body["total_cases"] == 0

    async def test_a_scorer_note_quoting_a_case_value_is_withheld(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        # The v13 tool scorer writes the forbidden argument value with %q.
        leaked = 'v13: argument city carries the forbidden "Lisbon" — case scored 0'
        agent_id, sha = await _seeded(session_maker)
        _install(app, session_maker)
        async with session_maker() as s, s.begin():
            score = (
                await s.scalars(select(Score).where(Score.agent_id == agent_id))
            ).one()
            details = dict(score.details or {})
            per_case = [dict(case) for case in details["per_case"]]
            per_case[1]["notes"] = [*per_case[1].get("notes", []), leaked]
            details["per_case"] = per_case
            await s.execute(
                update(Score).where(Score.agent_id == agent_id).values(details=details)
            )

        body = (await _get(client, agent_id, sha, case_id="memory-9f3a-0002")).json()

        (case,) = body["cases"]
        assert case["scorer_notes"][-1] == WITHHELD_SCORER_NOTE
        assert any("claim provenance flagged" in n for n in case["scorer_notes"])
        assert "Lisbon" not in json.dumps(body)

    @pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}])
    async def test_requires_the_admin_token(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        headers: dict[str, str],
    ) -> None:
        agent_id, sha = await _seeded(session_maker)
        _install(app, session_maker)

        resp = await client.get(
            _url(agent_id),
            params={"artifact_sha256": sha, "run_id": _RUN},
            headers=headers,
        )

        assert resp.status_code in (401, 403)
