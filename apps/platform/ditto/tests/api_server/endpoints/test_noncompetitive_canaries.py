"""Team canary exclusions: audited controls and fail-closed ledger surfaces."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import bittensor
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import ditto.api_server.endpoints.scoring as scoring_mod
from ditto.api_models.agent_status import AgentStatus
from ditto.api_server.dependencies import get_chain_client, get_session
from ditto.chain.models import NeuronInfo
from ditto.db.models import Agent, BenchmarkRollout
from ditto.db.queries.scores import MIN_ELIGIBLE_CASES, upsert_score

pytestmark = pytest.mark.asyncio

_ADMIN_TOKEN = "test-admin-token-at-least-32-characters"
_ADMIN = {
    "Authorization": f"Bearer {_ADMIN_TOKEN}",
    "X-Admin-Actor": "operator@example.com",
}
_KEYPAIR = bittensor.Keypair.create_from_uri("//Alice")
_VALIDATOR = _KEYPAIR.ss58_address
_CANARY = "5FKbkmKbJHTgsELVPigLJqbmovaviDN7dHZzX7UJ6xoqG4fx"
_ORDINARY = "5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm"
_ARTIFACT = "c1" * 32
_IMAGE = "e3" * 32
_VERSION = 7
_URL = "/api/v1/admin/noncompetitive-canaries"
_REASON = "team canary for hosted coding certification"


@pytest.fixture(autouse=True)
async def _active_era(session_maker: async_sessionmaker[AsyncSession]) -> None:
    async with session_maker() as session, session.begin():
        session.add(
            BenchmarkRollout(
                rollout_id=uuid4(),
                from_version=_VERSION - 1,
                desired_version=_VERSION,
                status="activated",
                cohort_size=5,
                activated_at=datetime(2026, 6, 1, tzinfo=UTC),
            )
        )


def _install(app: FastAPI, maker: async_sessionmaker[AsyncSession]) -> None:
    app.state.config = replace(app.state.config, admin_api_token=_ADMIN_TOKEN)

    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    async def _chain() -> MagicMock:
        chain = MagicMock()
        chain.get_recent_neurons = AsyncMock(
            return_value=[
                NeuronInfo(
                    hotkey=_VALIDATOR,
                    coldkey="5GReceiverColdkeyPlaceholderXXXXXXXXXXXXXXXXXXX",
                    uid=1,
                    stake=1000.0,
                    validator_permit=True,
                )
            ]
        )
        return chain

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_chain_client] = _chain


def _ledger_headers() -> dict[str, str]:
    nonce = uuid4()
    requested_at = datetime.now(UTC)
    requested = requested_at.astimezone(UTC).isoformat(timespec="microseconds")
    signed = f"validator-ledger:v1:{_VALIDATOR}:{nonce}:{requested}".encode()
    return {
        "X-Validator-Hotkey": _VALIDATOR,
        "X-Validator-Ledger-Nonce": str(nonce),
        "X-Validator-Ledger-Requested-At": requested_at.isoformat(),
        "X-Validator-Ledger-Signature": _KEYPAIR.sign(signed).hex(),
    }


async def _agent(
    maker: async_sessionmaker[AsyncSession],
    *,
    hotkey: str,
    sha256: str,
    composite: float,
) -> UUID:
    agent_id = uuid4()
    now = datetime(2026, 9, 1, tzinfo=UTC)
    async with maker() as session, session.begin():
        session.add(
            Agent(
                agent_id=agent_id,
                miner_hotkey=hotkey,
                name="agent",
                sha256=sha256,
                size_bytes=1024,
                status=AgentStatus.SCORED,
                created_at=now,
                screened_image_sha256=_IMAGE,
                screened_image_size_bytes=2048,
                screened_image_id="sha256:" + _IMAGE,
                screened_image_ref=f"ditto-screen/{agent_id}:latest",
                screened_image_upload_id=uuid4(),
                screened_image_verified_at=now,
            )
        )
        await session.flush()
        for index, validator in enumerate(("5Va", "5Vb", "5Vc")):
            await upsert_score(
                session,
                agent_id=agent_id,
                validator_hotkey=validator,
                bench_version=_VERSION,
                run_id=f"run-{agent_id}-{index}",
                seed=index,
                composite=composite,
                tool_mean=composite,
                memory_mean=composite,
                median_ms=500,
                n=MIN_ELIGIBLE_CASES,
                generated_at=now + timedelta(minutes=index),
                signature="ab" * 64,
            )
    return agent_id


def _reserve_body(**changes: object) -> dict[str, object]:
    body: dict[str, object] = {
        "miner_hotkey": _CANARY,
        "artifact_sha256": _ARTIFACT,
        "reason": _REASON,
        "confirmation": f"RESERVE TEAM CANARY {_CANARY} {_ARTIFACT}",
    }
    body.update(changes)
    return body


async def test_reserve_and_bind_are_audited_exact_and_once_only(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    empty = await client.get(_URL, headers=_ADMIN)
    assert empty.status_code == 200, empty.text
    assert empty.json() == {"total": 0, "exclusions": []}

    unauthenticated = await client.post(_URL, json=_reserve_body())
    assert unauthenticated.status_code in {401, 403}
    no_actor = await client.post(
        _URL, headers={"Authorization": _ADMIN["Authorization"]}, json=_reserve_body()
    )
    assert no_actor.status_code == 422
    wrong = await client.post(
        _URL, headers=_ADMIN, json=_reserve_body(confirmation="RESERVE TEAM CANARY")
    )
    assert wrong.status_code == 409
    long_reason = await client.post(
        _URL, headers=_ADMIN, json=_reserve_body(reason="r" * 5000)
    )
    assert long_reason.status_code == 201, long_reason.text
    reserved = long_reason.json()
    assert reserved["kind"] == "team_canary"
    assert reserved["agent_id"] is None and reserved["matched_agents"] == []
    duplicate = await client.post(_URL, headers=_ADMIN, json=_reserve_body())
    assert duplicate.status_code == 409

    canary = await _agent(
        session_maker, hotkey=_CANARY, sha256=_ARTIFACT, composite=0.9
    )
    exclusion_id = reserved["exclusion_id"]
    bind_url = f"{_URL}/{exclusion_id}/bind"

    def bind_body(**changes: object) -> dict[str, object]:
        body: dict[str, object] = {
            "agent_id": str(canary),
            "miner_hotkey": _CANARY,
            "artifact_sha256": _ARTIFACT,
            "screened_image_sha256": _IMAGE,
            "reason": "bound after the screened image was verified",
            "confirmation": f"BIND TEAM CANARY {exclusion_id} {canary}",
        }
        body.update(changes)
        return body

    listing = (await client.get(_URL, headers=_ADMIN)).json()
    assert [a["state"] for a in listing["exclusions"][0]["matched_agents"]] == [
        "reserved"
    ]
    missing = await client.post(
        f"{_URL}/{uuid4()}/bind", headers=_ADMIN, json=bind_body()
    )
    assert missing.status_code in {404, 409}
    drifted = await client.post(
        bind_url, headers=_ADMIN, json=bind_body(screened_image_sha256="f4" * 32)
    )
    assert drifted.status_code == 409
    bound = await client.post(bind_url, headers=_ADMIN, json=bind_body())
    assert bound.status_code == 200, bound.text
    assert bound.json()["bound_by"] == "operator@example.com"
    assert [a["state"] for a in bound.json()["matched_agents"]] == ["bound"]
    again = await client.post(bind_url, headers=_ADMIN, json=bind_body())
    assert again.status_code == 409


async def test_validator_ledger_drops_the_canary_but_keeps_ordinary_miners(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    reserved = await client.post(_URL, headers=_ADMIN, json=_reserve_body())
    assert reserved.status_code == 201, reserved.text
    canary = await _agent(
        session_maker, hotkey=_CANARY, sha256=_ARTIFACT, composite=0.95
    )
    ordinary = await _agent(
        session_maker, hotkey=_ORDINARY, sha256="a1" * 32, composite=0.5
    )
    response = await client.get("/api/v1/scoring/scores", headers=_ledger_headers())
    assert response.status_code == 200, response.text
    served = {entry["agent_id"] for entry in response.json()["entries"]}
    assert str(ordinary) in served
    assert str(canary) not in served


async def test_new_exclusion_invalidates_fresh_snapshot_and_stale_replay(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(app, session_maker)
    agent = await _agent(session_maker, hotkey=_CANARY, sha256=_ARTIFACT, composite=0.9)
    ordinary = await _agent(
        session_maker, hotkey=_ORDINARY, sha256="a1" * 32, composite=0.5
    )
    first = await client.get("/api/v1/scoring/scores", headers=_ledger_headers())
    assert str(agent) in {e["agent_id"] for e in first.json()["entries"]}

    # A late exclusion can only be written out of band (the service refuses a
    # scored artifact); the ledger must still never replay the identity.
    async with session_maker() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO noncompetitive_agent_exclusions "
                "(exclusion_id, kind, miner_hotkey, artifact_sha256, reason, "
                "created_by) VALUES (:id, 'team_canary', :hotkey, :sha, :reason, "
                "'operator@example.com')"
            ),
            {"id": uuid4(), "hotkey": _CANARY, "sha": _ARTIFACT, "reason": _REASON},
        )
    fresh = await client.get("/api/v1/scoring/scores", headers=_ledger_headers())
    assert {e["agent_id"] for e in fresh.json()["entries"]} == {str(ordinary)}

    # Replay an older snapshot that still contains the canary while the ledger
    # read fails after this request's context has seen the exclusion.
    app.state.ledger_snapshot = replace(
        app.state.ledger_snapshot,
        entries=[
            scoring_mod.LedgerEntry.model_validate(entry)
            for entry in first.json()["entries"]
        ],
        generated_at=datetime.now(UTC)
        - timedelta(seconds=scoring_mod._FRESH_SNAPSHOT_SECONDS + 1),
        context=None,
    )

    async def _boom(_session: object, **_kwargs: object) -> list:
        raise OperationalError("SELECT ...", {}, Exception("db down"))

    monkeypatch.setattr(scoring_mod, "list_eligible_ledger", _boom)
    stale = await client.get("/api/v1/scoring/scores", headers=_ledger_headers())
    assert stale.status_code == 200, stale.text
    assert stale.json()["stale"] is True
    assert {e["agent_id"] for e in stale.json()["entries"]} == {str(ordinary)}


async def test_public_board_labels_the_canary_without_rank_or_emissions(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    assert (
        await client.post(_URL, headers=_ADMIN, json=_reserve_body())
    ).status_code == 201
    canary = await _agent(
        session_maker, hotkey=_CANARY, sha256=_ARTIFACT, composite=0.95
    )
    ordinary = await _agent(
        session_maker, hotkey=_ORDINARY, sha256="a1" * 32, composite=0.5
    )
    board = await client.get("/api/v1/public/leaderboard")
    assert board.status_code == 200, board.text
    entries = {entry["agent_id"]: entry for entry in board.json()["entries"]}
    assert entries[str(canary)]["team_canary"] is True
    assert entries[str(canary)]["rank"] is None
    assert entries[str(canary)]["eligible"] is False
    assert entries[str(canary)]["emission_eligible"] in {False, None}
    assert entries[str(ordinary)]["team_canary"] is False
    assert entries[str(ordinary)]["rank"] == 1
