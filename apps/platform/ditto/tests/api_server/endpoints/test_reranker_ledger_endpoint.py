"""``GET /scoring/reranker-ledger``: the Season 2 reranker shadow ledger (SN-209).

Auth reuses the memory ledger's signed proof (helpers imported from
``test_scoring``). The shadow contract is what is unique: the ledger reports
the shadow crown and real measurements, and every entry and the champion are
clamped to ``weight_eligible=False`` / ``combined_score=0`` before serving.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from dittobench_reranker.contract import CONTRACT, CONTRACT_ID
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.reranker_ledger import (
    RerankerLedgerChampion,
    RerankerLedgerEntry,
    RerankerLedgerResponse,
)
from ditto.api_server.middleware.error_envelope import ERROR_CODE_VALIDATOR_AUTH
from ditto.api_server.reranker_ledger import clamp_reranker_ledger_to_shadow
from ditto.db.models import RerankerChampion, RerankerSubmission
from ditto.tests.api_server.endpoints.test_scoring import (
    _install_chain,
    _install_db,
    _ledger_headers,
)

_PATH = "/api/v1/scoring/reranker-ledger"
_HOTKEY = "5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm"
_T0 = datetime(2026, 10, 13, 12, tzinfo=UTC)


def _score(challenger: float, champion: float, *, crowned: bool) -> dict[str, Any]:
    return {
        "schema": "reranker-score-v1",
        "pools_sha256": "d" * 64,
        "challenger": {"score": challenger},
        "champion": {"kind": "incumbent", "score": champion},
        "comparison": {
            "mean_diff": challenger - champion,
            "threshold": 0.005,
            "dethrones": challenger - champion >= 0.005,
        },
        "latency": {"passed": True},
        "transfer_gate": {"status": "not_evaluated", "required": False},
        "crowned": crowned,
    }


async def _seed(
    session_maker: async_sessionmaker[AsyncSession],
    *,
    n: int,
    score: dict[str, Any] | None,
    status: str = "scored",
    at: datetime = _T0,
) -> UUID:
    submission_id = uuid4()
    async with session_maker() as session, session.begin():
        session.add(
            RerankerSubmission(
                id=submission_id,
                miner_hotkey=_HOTKEY,
                upload_sha256=f"{1000 + n:064x}",
                artifact_sha256=f"{n:064x}",
                contract_sha256=CONTRACT.digest(),
                size_bytes=10,
                base_model="none",
                base_model_license="mit",
                status=status,
                score=score,
                scored_at=at if score is not None else None,
            )
        )
    return submission_id


class TestRerankerLedgerEndpoint:
    async def test_empty_ledger_names_the_incumbent(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        _install_chain(app)
        resp = await client.get(_PATH, headers=_ledger_headers())
        assert resp.status_code == 200, resp.text
        assert resp.headers["Cache-Control"] == "no-store"
        body = RerankerLedgerResponse.model_validate(resp.json())
        assert body.contract_id == CONTRACT_ID
        assert body.champion == RerankerLedgerChampion(kind="incumbent")
        assert body.entries == [] and body.count == 0

    async def test_scored_rows_and_crown_are_served_clamped(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        _install_chain(app)
        crowned = await _seed(
            session_maker, n=1, score=_score(0.83, 0.79, crowned=True)
        )
        newer = await _seed(
            session_maker,
            n=2,
            score=_score(0.80, 0.83, crowned=False),
            at=_T0 + timedelta(hours=1),
        )
        await _seed(session_maker, n=3, score=None, status="accepted")
        # A foreign score shape is skipped, not a 500.
        await _seed(
            session_maker, n=4, score={"schema": "other"}, at=_T0 + timedelta(hours=2)
        )
        async with session_maker() as session, session.begin():
            session.add(
                RerankerChampion(
                    submission_id=crowned,
                    crowned_at=_T0,
                    contract_sha256=CONTRACT.digest(),
                    pools_sha256="d" * 64,
                    comparison={"dethrones": True},
                )
            )

        resp = await client.get(_PATH, headers=_ledger_headers())
        assert resp.status_code == 200, resp.text
        body = RerankerLedgerResponse.model_validate(resp.json())
        assert body.champion is not None
        assert body.champion.kind == "submission"
        assert body.champion.submission_id == crowned
        assert [e.submission_id for e in body.entries] == [newer, crowned]
        assert body.count == 2
        assert body.entries[1].shadow_score == pytest.approx(0.83)
        assert body.entries[1].crowned is True
        assert body.entries[0].dethrones is False
        assert body.champion.weight_eligible is False
        assert body.champion.combined_score == 0.0
        for entry in body.entries:
            assert entry.weight_eligible is False
            assert entry.combined_score == 0.0

    async def test_auth_is_the_memory_ledger_proof(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        _install_chain(app)
        missing = await client.get(_PATH)
        assert missing.status_code == 401
        assert missing.json()["error_code"] == ERROR_CODE_VALIDATOR_AUTH
        headers = _ledger_headers()
        assert (await client.get(_PATH, headers=headers)).status_code == 200
        assert (await client.get(_PATH, headers=headers)).status_code == 409

    async def test_unpermitted_validator_returns_401(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        _install_chain(app, permitted=False)
        resp = await client.get(_PATH, headers=_ledger_headers())
        assert resp.status_code == 401


def test_clamp_overrides_weight_bearing_values() -> None:
    entry = RerankerLedgerEntry(
        submission_id=uuid4(),
        miner_hotkey=_HOTKEY,
        artifact_sha256="a" * 64,
        status="scored",
        shadow_score=0.9,
        champion_score=0.8,
        champion_kind="incumbent",
        mean_diff=0.1,
        threshold=0.005,
        dethrones=True,
        latency_passed=True,
        transfer_gate="passed",
        crowned=True,
        pools_sha256="d" * 64,
        scored_at=_T0,
        weight_eligible=True,
        combined_score=0.9,
    )
    champion = RerankerLedgerChampion(
        kind="submission",
        submission_id=entry.submission_id,
        miner_hotkey=_HOTKEY,
        weight_eligible=True,
        combined_score=1.0,
    )
    clamped = clamp_reranker_ledger_to_shadow(
        RerankerLedgerResponse(champion=champion, entries=[entry], count=1)
    )
    assert clamped.champion is not None
    assert (clamped.champion.weight_eligible, clamped.champion.combined_score) == (
        False,
        0.0,
    )
    (only,) = clamped.entries
    assert (only.weight_eligible, only.combined_score) == (False, 0.0)
    assert only.shadow_score == pytest.approx(0.9)
