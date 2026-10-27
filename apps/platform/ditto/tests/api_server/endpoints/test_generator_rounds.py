"""Generator-competition rounds: the chain-epoch agent x generator matrix.

Real Postgres end to end: the epoch-driven lifecycle (commit ahead, open on the
chain epoch with a seed bound to the committed secret and the open block,
generate, run, close, score, reveal), the scorer's round work, the validator
lane that runs panel agents on ``generator_round`` tickets, the round ledger,
and the operator and public views. The derivations are pinned against the Go
contract's golden vectors (``challenge/testdata/round_vectors.json``).
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import bittensor
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import ditto.api_server.endpoints.validator_generator_round as lane_mod
import ditto.api_server.generator_competition as competition_mod
import ditto.db.queries.tickets as tickets_mod
from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.generator import generator_settings_confirmation
from ditto.api_models.generator_round_validator import (
    MAX_ROUND_COLUMN_CASES,
    GeneratorRoundCaseResult,
    GeneratorRoundFailRequest,
    GeneratorRoundJobRequest,
    GeneratorRoundScoreRequest,
)
from ditto.api_models.generator_rounds import (
    REGISTER_DEFAULT_GENERATOR_CONFIRMATION,
    RELEASE_ROUND_LEASE_CONFIRMATION,
    onchain_commitment_payload,
    void_round_confirmation,
)
from ditto.api_models.private_dataset import PrivateDatasetRequest
from ditto.api_models.screener import SCREENING_POLICY_VERSION
from ditto.api_models.ticket_status import TicketPurpose, TicketStatus
from ditto.api_server.dependencies import get_chain_client, get_session
from ditto.api_server.generator_competition import load_settings
from ditto.api_server.generator_rounds import (
    ROUND_LEASE_CUTOFF,
    ChainPosition,
    GeneratorRoundLoop,
    advance_rounds,
    get_round,
    round_column_seed,
    round_commitment,
    round_lease_cutoff,
    round_seconds_left,
    round_seed,
)
from ditto.chain.models import EpochSchedule, NeuronInfo
from ditto.db.models import (
    Agent,
    BannedHotkey,
    GeneratorOperatorAction,
    GeneratorRound,
    GeneratorRoundAttempt,
    GeneratorRoundCaseVerdict,
    GeneratorSubmission,
    ProviderOutageCircuit,
    ValidatorTicket,
)
from ditto.db.queries.generator import claimed_fingerprints
from ditto.db.queries.provider_outages import park_scoring_leases
from ditto.tests.api_server.endpoints.test_generator_competition import (
    _ADMIN,
    _ADMIN_TOKEN,
    _SCORER,
    _SCORER_TOKEN,
    _archive,
    _settings,
)

_REPO = Path(__file__).resolve().parents[6]
_VECTORS = _REPO / "research/dittobench-datagen/challenge/testdata/round_vectors.json"
_VALIDATOR = bittensor.Keypair.create_from_uri("//Alice")
_SETTINGS_URL = "/api/v1/admin/generator-competition/settings"
_ROUNDS_URL = "/api/v1/admin/generator-competition/rounds"
_BLOCK = "0x" + "5a" * 32


def _install(app: FastAPI, maker: async_sessionmaker[AsyncSession]) -> MagicMock:
    app.state.config = replace(
        app.state.config,
        admin_api_token=_ADMIN_TOKEN,
        generator_scorer_token=_SCORER_TOKEN,
    )
    app.state.session_maker = maker
    _LOOP_EPOCH["epoch"] = 0

    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    chain = MagicMock()
    chain.get_recent_neurons = AsyncMock(
        return_value=[
            NeuronInfo(
                hotkey=_VALIDATOR.ss58_address,
                coldkey="5GReceiverColdkeyPlaceholderXXXXXXXXXXXXXXXXXXX",
                uid=1,
                stake=1000.0,
                validator_permit=True,
            )
        ]
    )
    # The lease endpoint reads the head at lease time. By default it is the
    # position the test last advanced the round loop to, five blocks into
    # that epoch, as on chain. Tests that need another head override this.
    chain.read_epoch_schedule = AsyncMock(
        side_effect=lambda _netuid: _head_at(_LOOP_EPOCH["epoch"])
    )

    async def _chain() -> MagicMock:
        return chain

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_chain_client] = _chain
    app.state.chain = chain
    return chain


async def _apply_settings(
    client: httpx.AsyncClient, *, status: int = 200, **overrides: Any
) -> httpx.Response:
    current = (await client.get(_SETTINGS_URL, headers=_ADMIN)).json()
    settings = _settings(**{"min_panel": 3, "panel_size": 3, **overrides})
    resp = await client.post(
        _SETTINGS_URL,
        headers=_ADMIN,
        json={
            "expected_revision": current["effective"]["revision"],
            "settings": settings,
            "reason": "run generator rounds in shadow",
            "actor": "backroom:test",
            "confirmation": generator_settings_confirmation(settings["mode"]),
        },
    )
    assert resp.status_code == status, resp.text
    return resp


async def _seed_agent(maker: async_sessionmaker[AsyncSession], rank: int) -> UUID:
    agent_id = uuid4()
    now = datetime.now(UTC)
    async with maker() as session, session.begin():
        session.add(
            Agent(
                agent_id=agent_id,
                miner_hotkey=f"5PanelMiner{rank}",
                name=f"panel-{rank}",
                sha256=f"{rank + 10:02d}" * 32,
                size_bytes=1024,
                status=AgentStatus.SCORED,
                screening_policy_version=SCREENING_POLICY_VERSION,
                created_at=now,
                screened_image_sha256="12" * 32,
                screened_image_size_bytes=123,
                screened_image_id="sha256:" + "34" * 32,
                screened_image_ref=f"ditto-screen/{agent_id}:latest",
                screened_image_upload_id=uuid4(),
                screened_image_verified_at=now,
            )
        )
    return agent_id


async def _seed_generator(
    maker: async_sessionmaker[AsyncSession],
    *,
    hotkey: str,
    owner: str,
    salt: str,
    qualified: bool = True,
) -> UUID:
    raw = _archive(salt)
    now = datetime.now(UTC)
    row = GeneratorSubmission(
        submission_id=uuid4(),
        miner_hotkey=hotkey,
        owner_key=owner,
        source_sha256=hashlib.sha256(raw).hexdigest(),
        tree_sha256=hashlib.sha256(salt.encode()).hexdigest(),
        source_bytes=raw,
        size_bytes=len(raw),
        module="example.com/gen",
        go_version="1.23",
        file_count=2,
        status="active",
        attempts=0,
        next_run_at=now,
        submitted_at=now - timedelta(hours=1),
        updated_at=now,
        qualified_at=now if qualified else None,
    )
    async with maker() as session, session.begin():
        session.add(row)
    return row.submission_id


async def _register_default(client: httpx.AsyncClient) -> str:
    raw = _archive("public-default")
    resp = await client.post(
        "/api/v1/admin/generator-competition/default-generator",
        headers=_ADMIN,
        json={
            "miner_hotkey": _VALIDATOR.ss58_address,
            "source_sha256": hashlib.sha256(raw).hexdigest(),
            "source_base64": base64.b64encode(raw).decode(),
            "reason": "the public default generator",
            "actor": "backroom:test",
            "confirmation": REGISTER_DEFAULT_GENERATOR_CONFIRMATION,
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["submission_id"]


async def _qualify_all(maker: async_sessionmaker[AsyncSession]) -> None:
    async with maker() as session, session.begin():
        for row in await session.scalars(select(GeneratorSubmission)):
            row.qualified_at = row.qualified_at or datetime.now(UTC)


@pytest.fixture(autouse=True)
def round_capable_fleet(monkeypatch: pytest.MonkeyPatch) -> None:
    """One validator can run every round contract, unless a test says not.

    Opening a round needs a validator able to run its contract; the tests
    below that exercise that gate replace this stand-in themselves.
    """
    import ditto.api_server.generator_rounds as rounds_mod

    async def _capable(*_args: Any, **_kwargs: Any) -> int:
        return 1

    monkeypatch.setattr(rounds_mod, "round_capable_validators", _capable)


_LOOP_EPOCH = {"epoch": 0}


def _head_at(epoch: int) -> EpochSchedule:
    last = 1000 + epoch * 360
    return EpochSchedule(
        netuid=118,
        subnet_epoch_index=epoch,
        last_epoch_block=last,
        pending_epoch_at=0,
        tempo=360,
        blocks_since_last_step=5,
        block=last + 5,
        block_hash=_BLOCK,
        block_timestamp=None,
        next_epoch_block=last + 360,
    )


def _position(epoch: int, *, block_hash: str = _BLOCK) -> ChainPosition:
    return ChainPosition(
        subnet_epoch_index=epoch,
        last_epoch_block=1000 + epoch * 360,
        last_epoch_block_hash=block_hash,
        tempo=360,
        block=1000 + epoch * 360 + 5,
    )


async def _advance(
    maker: async_sessionmaker[AsyncSession], epoch: int, **kwargs: Any
) -> list[str]:
    _LOOP_EPOCH["epoch"] = epoch
    async with maker() as session, session.begin():
        resolved = await load_settings(session)
        out = await advance_rounds(
            session,
            resolved=resolved,
            position=_position(epoch, **kwargs),
            now=datetime.now(UTC),
        )
    return out.actions


def _patch_panel(monkeypatch: pytest.MonkeyPatch, agents: list[UUID]) -> None:
    async def _select(_session: AsyncSession, *, size: int) -> list[dict[str, Any]]:
        return [
            {
                "agent_id": str(agent),
                "owner_key": f"coldkey:{index}",
                "miner_hotkey": f"5PanelMiner{index}",
                "champion": index == 0,
            }
            for index, agent in enumerate(agents[:size])
        ]

    monkeypatch.setattr(competition_mod, "select_panel", _select)


def _patch_lane(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stand in for the fleet gates the canonical lanes already test."""

    async def _allowed(*_args: Any, **_kwargs: Any) -> bool:
        return True

    async def _compatible(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def _slot(*_args: Any, **_kwargs: Any) -> str:
        return "slot-0"

    async def _grant(_session: AsyncSession, *, ticket: ValidatorTicket, **_: Any):
        return SimpleNamespace(
            grant_id=uuid4(),
            allowed_models=["model"],
            request_budget=100,
            token_budget=1000,
            expires_at=ticket.deadline,
            route_provider=None,
            route_profile=None,
        )

    async def _revoke(*_args: Any, **_kwargs: Any) -> None:
        return None

    import ditto.api_server.v13_scorer_cohort as cohort_mod

    monkeypatch.setattr(cohort_mod, "pinned_validator_allowed", _allowed)
    monkeypatch.setattr(
        lane_mod, "_round_column_capacity", lambda *_a, **_k: MAX_ROUND_COLUMN_CASES
    )
    monkeypatch.setattr(lane_mod.v, "_assert_validator_compatible", _compatible)
    monkeypatch.setattr(lane_mod.v, "_idle_retest_slot", _slot)
    monkeypatch.setattr(lane_mod, "ensure_inference_grant", _grant)
    monkeypatch.setattr(lane_mod, "revoke_ticket_inference", _revoke)
    import ditto.api_server.generator_rounds as rounds_mod
    import ditto.db.queries.inference as inference_mod

    monkeypatch.setattr(inference_mod, "revoke_ticket_inference", _revoke)
    del rounds_mod


def _generation(work: dict[str, Any], *, failing: frozenset[str] = frozenset()) -> dict:
    columns, packs = [], {}
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
        ok = cid not in failing
        columns.append(
            {
                "column_id": cid,
                "submission_id": column["submission_id"],
                "ok": ok,
                "build": {"ok": True, "duration_ms": 5},
                "seed": {
                    "seed": seed,
                    "validation": {
                        "valid": ok,
                        "case_count": len(refs),
                        "cases": refs,
                    },
                    "error": None if ok else "generator generate: exit",
                    "duration_ms": 7,
                },
                "error": None if ok else "generator generate: exit",
            }
        )
        if ok:
            packs[cid] = refs
    dataset = json.dumps(
        {"round": work["round_index"], "packs": packs}, sort_keys=True
    ).encode()
    return {
        "lease_id": work["lease_id"],
        "round_index": work["round_index"],
        "scorer_version": "challenge-scorer/3",
        "sandbox": "docker",
        "columns": columns,
        "dataset_sha256": hashlib.sha256(dataset).hexdigest(),
        "dataset_base64": base64.b64encode(dataset).decode(),
    }


def _signed_job() -> dict[str, Any]:
    payload = GeneratorRoundJobRequest(
        validator_hotkey=_VALIDATOR.ss58_address,
        slot_id="slot-0",
        nonce=uuid4(),
        requested_at=datetime.now(UTC),
    )
    payload.signature = _VALIDATOR.sign(payload.signing_message()).hex()
    return payload.model_dump(mode="json")


def _signed_score(
    job: dict[str, Any], case_ids: list[str], score: float, *, fault: bool = False
) -> dict:
    payload = GeneratorRoundScoreRequest(
        validator_hotkey=_VALIDATOR.ss58_address,
        round_index=job["round_index"],
        ticket_deadline=datetime.fromisoformat(job["deadline"]),
        dataset_sha256=job["dataset_sha256"],
        run_id=f"run-{job['agent_id']}",
        results=[
            GeneratorRoundCaseResult(
                case_id=cid, score=0.0 if fault else score, fault=fault
            )
            for cid in case_ids
        ],
    )
    payload.signature = _VALIDATOR.sign(
        payload.signing_message(UUID(job["agent_id"]))
    ).hex()
    return payload.model_dump(mode="json")


def _signed_fail(job: dict[str, Any], *, round_index: int | None = None) -> dict:
    payload = GeneratorRoundFailRequest(
        validator_hotkey=_VALIDATOR.ss58_address,
        round_index=job["round_index"] if round_index is None else round_index,
        ticket_deadline=datetime.fromisoformat(job["deadline"]),
        reason="scoring_error",
        nonce=uuid4(),
        requested_at=datetime.now(UTC),
    )
    payload.signature = _VALIDATOR.sign(
        payload.signing_message(UUID(job["agent_id"]))
    ).hex()
    return payload.model_dump(mode="json")


def _matrix_score(work: dict[str, Any]) -> dict[str, Any]:
    """A ScoreRound-shaped result for the leased matrix (Go computes the real
    one; this test pins what Platform persists from it)."""
    columns = []
    for index, column in enumerate(work["matrix"]["columns"]):
        cases = [
            {
                "case_id": ref["case_id"],
                "fingerprint": ref["fingerprint"],
                "status": "discriminating",
                "panel_size": 3,
                "solvers": 2,
                "solver_owners": 2,
                "mean_score": 0.5,
                "champion_failed": False,
                "credit": 0.5,
            }
            for ref in column["cases"]
        ]
        columns.append(
            {
                "column_id": column["column_id"],
                "pack": {
                    "status": "scored",
                    "score": 0.1 * (index + 1),
                    "raw_credit": 0.5 * len(cases),
                    "discriminating": len(cases),
                    "trivial": 0,
                    "unsolved": 0,
                    "duplicate": 0,
                    "unsolved_rate": 0,
                    "cases": cases,
                },
            }
        )
    agents = [
        {
            "agent_id": agent["agent_id"],
            "rank": rank + 1,
            "pairwise_score": 1 - rank / 2,
            "wins": 2 - rank,
            "ties": 0,
            "losses": rank,
            "opponents": 2,
            "common_mean": 0.5,
            "common_cases": 8,
        }
        for rank, agent in enumerate(work["matrix"]["agents"])
    ]
    return {
        "lease_id": work["lease_id"],
        "round_index": work["round_index"],
        "scorer_version": "challenge-scorer/3",
        "score": {"columns": columns, "agents": agents},
    }


_JOB_URL = "/api/v1/validator/generator-round-job"


async def _generate_round(client: httpx.AsyncClient) -> list[str]:
    """Generate the leased round's dataset; returns its case ids."""
    work = (
        await client.get("/api/v1/generator-scorer/round-work", headers=_SCORER)
    ).json()["work"]
    assert work["kind"] == "generate"
    generated = _generation(work)
    resp = await client.post(
        "/api/v1/generator-scorer/round-results", headers=_SCORER, json=generated
    )
    assert resp.status_code == 200 and resp.json()["status"] == "running", resp.text
    return [
        f"{column['column_id']}/{ref['case_id']}"
        for column in generated["columns"]
        for ref in column["seed"]["validation"]["cases"]
    ]


async def _lease_job(client: httpx.AsyncClient) -> dict[str, Any] | None:
    resp = await client.post(
        _JOB_URL,
        headers={"X-Validator-Hotkey": _VALIDATOR.ss58_address},
        json=_signed_job(),
    )
    if resp.status_code == 204:
        return None
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _report(
    client: httpx.AsyncClient,
    job: dict[str, Any],
    case_ids: list[str],
    *,
    fault: bool = False,
) -> None:
    resp = await client.post(
        f"/api/v1/validator/agent/{job['agent_id']}/generator-round-score",
        json=_signed_score(job, case_ids, 0.75, fault=fault),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["accepted"] is True


async def _ban_agent(maker: async_sessionmaker[AsyncSession], agent_id: UUID) -> None:
    async with maker() as session, session.begin():
        agent = await session.get(Agent, agent_id)
        assert agent is not None
        agent.status = AgentStatus.BANNED


async def _ban_hotkey(maker: async_sessionmaker[AsyncSession], hotkey: str) -> None:
    async with maker() as session, session.begin():
        session.add(BannedHotkey(hotkey=hotkey, reason="test ban"))


def test_derivations_match_the_go_contract() -> None:
    vectors = json.loads(_VECTORS.read_text())
    assert vectors["derivations"]
    for v in vectors["derivations"]:
        secret = bytes.fromhex(v["secret_hex"])
        assert round_commitment(v["round"], secret) == v["commitment"]
        seed = round_seed(v["round"], secret, v["open_block_hash"])
        assert seed == v["seed"]
        for column_id, column_seed in v["column_seeds"].items():
            assert round_column_seed(seed, column_id) == column_seed
    assert onchain_commitment_payload(7, "ab" * 32) == b"dgr1:7:" + b"ab" * 32
    assert len(onchain_commitment_payload(2**40, "ab" * 32)) <= 128


class TestRoundLifecycle:
    async def test_full_round(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        chain = _install(app, session_maker)
        await _apply_settings(client, validator_rounds_enabled=True)
        agents = [await _seed_agent(session_maker, rank) for rank in range(3)]
        _patch_panel(monkeypatch, agents)
        _patch_lane(monkeypatch)
        default_id = await _register_default(client)
        miner_a = await _seed_generator(
            session_maker, hotkey="5GenMinerA", owner="coldkey:ga", salt="a"
        )
        miner_b = await _seed_generator(
            session_maker, hotkey="5GenMinerB", owner="coldkey:gb", salt="b"
        )
        await _qualify_all(session_maker)

        # First tick: round 2 (epochs 8..11) was never committed, so it is
        # void; round 3 is committed a round ahead.
        actions = await _advance(session_maker, 9)
        assert "round 2: void (not committed in time)" in actions
        assert "round 3: committed" in actions
        async with session_maker() as session:
            committed = await get_round(session, 3)
            assert committed is not None and committed.status == "committed"
            assert committed.commitment == round_commitment(3, committed.secret)
            secret = committed.secret
        # Ticking again at the same position changes nothing.
        assert await _advance(session_maker, 9) == []

        # Round 3 opens on its first epoch: the seed binds the committed
        # secret to the block the epoch fired at.
        await _advance(session_maker, 12)
        async with session_maker() as session:
            row = await get_round(session, 3)
            assert row is not None and row.status == "generating"
            assert row.seed == round_seed(3, secret, _BLOCK)
            assert row.open_epoch_index == 12 and row.open_block_hash == _BLOCK
            columns = row.columns or []
            assert [c["counted"] for c in columns] == [True] * 5
            ids = [c["submission_id"] for c in columns]
            assert ids[:2] == sorted([str(miner_a), str(miner_b)], key=ids.index)
            assert {ids[0], ids[1]} == {str(miner_a), str(miner_b)}
            assert ids[2:] == [default_id] * 3
            assert [c["owner_key"] for c in columns[2:]] == [""] * 3
            assert len(row.panel or []) == 3
            assert (await get_round(session, 4)) is not None

        # Public: no secret or seed before the reveal.
        public = (await client.get("/api/v1/generator/rounds/3")).json()
        assert public["summary"]["secret_hex"] is None
        assert public["summary"]["seed"] is None
        assert public["summary"]["commitment"] == round_commitment(3, secret)

        # Scorer generates the dataset.
        work = (
            await client.get("/api/v1/generator-scorer/round-work", headers=_SCORER)
        ).json()["work"]
        assert work["kind"] == "generate" and work["round_index"] == 3
        assert len(work["columns"]) == 5
        assert base64.b64decode(work["columns"][0]["source_base64"])[:2] == b"\x1f\x8b"
        assert (
            await client.get("/api/v1/generator-scorer/round-work", headers=_SCORER)
        ).json()["work"] is None
        generated = _generation(work)
        resp = await client.post(
            "/api/v1/generator-scorer/round-results", headers=_SCORER, json=generated
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "running"
        replay = await client.post(
            "/api/v1/generator-scorer/round-results", headers=_SCORER, json=generated
        )
        assert replay.status_code == 409

        # No lease too close to the round's close: closing revokes the run's
        # inference grant mid-seed, so the run could only fault every case.
        # The head is read at lease time; an unreadable chain declines too.
        async with session_maker() as session:
            row = await get_round(session, 3)
            assert row is not None and row.open_block is not None
            close_block = row.open_block + row.epochs_per_round * 360
        first = row.round_index * row.epochs_per_round
        last_epoch = first + row.epochs_per_round - 1
        for read in (
            AsyncMock(
                return_value=_head(
                    epoch=last_epoch,
                    last_epoch_block=close_block - 360,
                    block=close_block - 10,
                )
            ),
            AsyncMock(side_effect=RuntimeError("rpc down")),
        ):
            chain.read_epoch_schedule = read
            declined = await client.post(
                "/api/v1/validator/generator-round-job",
                headers={"X-Validator-Hotkey": _VALIDATOR.ss58_address},
                json=_signed_job(),
            )
            assert declined.status_code == 204, declined.text
        chain.read_epoch_schedule = AsyncMock(
            return_value=_head(
                epoch=first, last_epoch_block=row.open_block, block=row.open_block + 5
            )
        )

        # Validators run every panel agent on round tickets.
        case_ids: list[str] = []
        for column in generated["columns"]:
            case_ids += [
                f"{column['column_id']}/{ref['case_id']}"
                for ref in column["seed"]["validation"]["cases"]
            ]
        seen: set[str] = set()
        for index in range(3):
            job_resp = await client.post(
                "/api/v1/validator/generator-round-job",
                headers={"X-Validator-Hotkey": _VALIDATOR.ss58_address},
                json=_signed_job(),
            )
            assert job_resp.status_code == 200, job_resp.text
            job = job_resp.json()
            assert job["dataset_sha256"] == generated["dataset_sha256"]
            assert job["case_count"] == 40 and job["seed"] >= 1
            assert job["agent_id"] not in seen
            seen.add(job["agent_id"])
            async with session_maker() as session:
                ticket = await session.get(
                    ValidatorTicket,
                    (UUID(job["agent_id"]), 13, _VALIDATOR.ss58_address),
                )
                assert ticket is not None
                assert ticket.purpose == TicketPurpose.GENERATOR_ROUND
                # A round-created row has had no scoring lease (SN-334).
                assert ticket.attempt_count == 0
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
                # Results must cover exactly the round's cases.
                partial = _signed_score(job, case_ids[:-1], 1.0)
                assert (
                    await client.post(
                        f"/api/v1/validator/agent/{job['agent_id']}/generator-round-score",
                        json=partial,
                    )
                ).status_code == 422
            scored = await client.post(
                f"/api/v1/validator/agent/{job['agent_id']}/generator-round-score",
                json=_signed_score(job, case_ids, 1.0 - index * 0.4),
            )
            assert scored.status_code == 200, scored.text
            assert scored.json()["accepted"] is True
        # Every panel agent reported, so the round closed early.
        async with session_maker() as session:
            row = await get_round(session, 3)
            assert row is not None and row.status == "scoring"
        assert (
            await client.post(
                "/api/v1/validator/generator-round-job",
                headers={"X-Validator-Hotkey": _VALIDATOR.ss58_address},
                json=_signed_job(),
            )
        ).status_code == 204

        detail = (await client.get(f"{_ROUNDS_URL}/3", headers=_ADMIN)).json()
        assert len(detail["results"]) == 3 and detail["pending_agents"] == []
        assert {a["status"] for a in detail["attempts"]} == {"completed"}
        assert detail["seed"] == round_seed(3, secret, _BLOCK)
        assert detail["summary"]["dataset_sha256"] == generated["dataset_sha256"]

        # Scorer scores the matrix.
        work = (
            await client.get("/api/v1/generator-scorer/round-work", headers=_SCORER)
        ).json()["work"]
        assert work["kind"] == "score"
        assert len(work["matrix"]["agents"]) == 3
        assert len(work["matrix"]["columns"]) == 5
        assert work["matrix"]["claimed_fingerprints"] == []
        assert work["params"]["target_cases"] == 8
        resp = await client.post(
            "/api/v1/generator-scorer/round-score",
            headers=_SCORER,
            json=_matrix_score(work),
        )
        assert resp.status_code == 200, resp.text
        async with session_maker() as session:
            row = await get_round(session, 3)
            assert row is not None and row.status == "scored"
            a = await session.get(GeneratorSubmission, miner_a)
            assert a is not None and a.round_scored_index == 3
            assert a.round_score is not None

        ledger = (await client.get("/api/v1/generator/board")).json()
        assert {e["miner_hotkey"] for e in ledger["entries"]} == {
            "5GenMinerA",
            "5GenMinerB",
        }
        assert all(e["round_index"] == 3 for e in ledger["entries"])
        assert all(e["weight_eligible"] is False for e in ledger["entries"])
        # ``enforce`` marks the same qualified miner generators eligible and
        # changes nothing else about the ledger.
        await _apply_settings(client, mode="enforce")
        enforced = (await client.get("/api/v1/generator/board")).json()
        assert enforced["mode"] == "enforce"
        assert [(e["miner_hotkey"], e["score"]) for e in enforced["entries"]] == [
            (e["miner_hotkey"], e["score"]) for e in ledger["entries"]
        ]
        assert all(e["weight_eligible"] is True for e in enforced["entries"])
        # An unqualified generator is never paid, even with a scored column.
        async with session_maker() as session:
            a = await session.get(GeneratorSubmission, miner_a)
            assert a is not None
            qualified_at, a.qualified_at = a.qualified_at, None
            await session.commit()
        unqualified = (await client.get("/api/v1/generator/board")).json()
        assert {
            e["miner_hotkey"]: e["weight_eligible"] for e in unqualified["entries"]
        } == {"5GenMinerA": False, "5GenMinerB": True}
        async with session_maker() as session:
            a = await session.get(GeneratorSubmission, miner_a)
            assert a is not None
            a.qualified_at = qualified_at
            await session.commit()
        # A round scored before the freshness window stops counting.
        async with session_maker() as session:
            row = await get_round(session, 3)
            assert row is not None and row.scored_at is not None
            scored_at = row.scored_at
            row.scored_at = scored_at - timedelta(days=ledger["window_days"] + 1)
            await session.commit()
        stale = (await client.get("/api/v1/generator/board")).json()
        assert stale["entries"] == [] and stale["count"] == 0
        async with session_maker() as session:
            row = await get_round(session, 3)
            assert row is not None
            row.scored_at = scored_at
            await session.commit()
        await _apply_settings(client, mode="shadow")
        public = (await client.get("/api/v1/generator/rounds/3")).json()
        assert len(public["standings"]) == 3
        assert public["columns"][2]["miner_hotkey"] is None
        candidates = (
            await client.get(
                "/api/v1/admin/generator-competition/promotion-candidates",
                headers=_ADMIN,
            )
        ).json()["candidates"]
        assert len(candidates) == 40 and candidates[0]["round_index"] == 3

        # A rejected generator's round cases stop claiming their fingerprints.
        async with session_maker() as session:
            held = list(
                await session.scalars(
                    select(GeneratorRoundCaseVerdict.fingerprint).where(
                        GeneratorRoundCaseVerdict.submission_id == miner_a,
                        GeneratorRoundCaseVerdict.status == "discriminating",
                    )
                )
            )
            assert held
            assert await claimed_fingerprints(session, fingerprints=held) == sorted(
                set(held)
            )
            a = await session.get(GeneratorSubmission, miner_a)
            assert a is not None
            a.status = "rejected"
            await session.flush()
            assert await claimed_fingerprints(session, fingerprints=held) == []
            await session.rollback()

        # A later round claims round 3's fingerprints as duplicates.
        await _advance(session_maker, 16)
        async with session_maker() as session:
            nxt = await get_round(session, 4)
            assert nxt is not None and nxt.status == "generating"
            # Scored generators fill counted columns first, best score first.
            assert [c["submission_id"] for c in nxt.columns or []][:2] == [
                str(miner_b),
                str(miner_a),
            ] or [c["submission_id"] for c in nxt.columns or []][:2] == [
                str(miner_a),
                str(miner_b),
            ]

        # Reveal after the privacy window (6 rounds).
        await _advance(session_maker, 36)
        public = (await client.get("/api/v1/generator/rounds/3")).json()
        assert public["summary"]["secret_hex"] == secret.hex()
        assert public["summary"]["seed"] == round_seed(3, secret, _BLOCK)
        listing = (await client.get("/api/v1/generator/rounds")).json()
        assert listing["rounds"][0]["round_index"] >= 9

    async def test_round_generation_failure_rejects_miner_columns_only(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install(app, session_maker)
        await _apply_settings(client, counted_columns=2, challenger_columns=0)
        agents = [await _seed_agent(session_maker, rank) for rank in range(3)]
        _patch_panel(monkeypatch, agents)
        default_id = await _register_default(client)
        miner = await _seed_generator(
            session_maker, hotkey="5GenMinerA", owner="coldkey:ga", salt="a"
        )
        await _qualify_all(session_maker)
        await _advance(session_maker, 3)
        await _advance(session_maker, 4)
        work = (
            await client.get("/api/v1/generator-scorer/round-work", headers=_SCORER)
        ).json()["work"]
        assert [c["submission_id"] for c in work["columns"]] == [
            str(miner),
            default_id,
        ]
        generated = _generation(work, failing=frozenset({"c0"}))
        resp = await client.post(
            "/api/v1/generator-scorer/round-results", headers=_SCORER, json=generated
        )
        assert resp.status_code == 200 and resp.json()["status"] == "running"
        async with session_maker() as session:
            row = await session.get(GeneratorSubmission, miner)
            assert row is not None and row.status == "rejected"
            assert (row.status_reason or "").startswith("round_generation_failed")
            default = await session.get(GeneratorSubmission, UUID(default_id))
            assert default is not None and default.status == "active"
            rnd = await get_round(session, 1)
            assert rnd is not None and set(rnd.column_cases or {}) == {"c1"}

    async def test_round_end_without_enough_results_voids_and_frees_leases(
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
        _patch_lane(monkeypatch)
        await _register_default(client)
        await _qualify_all(session_maker)
        await _advance(session_maker, 3)
        await _advance(session_maker, 4)
        work = (
            await client.get("/api/v1/generator-scorer/round-work", headers=_SCORER)
        ).json()["work"]
        await client.post(
            "/api/v1/generator-scorer/round-results",
            headers=_SCORER,
            json=_generation(work),
        )
        job = (
            await client.post(
                "/api/v1/validator/generator-round-job",
                headers={"X-Validator-Hotkey": _VALIDATOR.ss58_address},
                json=_signed_job(),
            )
        ).json()

        # A failure that names another round changes nothing.
        misnamed = await client.post(
            f"/api/v1/validator/agent/{job['agent_id']}/generator-round-fail",
            headers={"X-Validator-Hotkey": _VALIDATOR.ss58_address},
            json=_signed_fail(job, round_index=99),
        )
        assert misnamed.status_code == 409, misnamed.text
        async with session_maker() as session:
            attempt = await session.scalar(
                select(GeneratorRoundAttempt).where(
                    GeneratorRoundAttempt.agent_id == UUID(job["agent_id"])
                )
            )
            assert attempt is not None and attempt.status == "leased"

        # Operator releases the quiet validator's lease.
        released = await client.post(
            f"{_ROUNDS_URL}/1/release-lease",
            headers=_ADMIN,
            json={
                "agent_id": job["agent_id"],
                "validator_hotkey": _VALIDATOR.ss58_address,
                "reason": "validator went quiet mid-run",
                "actor": "backroom:test",
                "confirmation": RELEASE_ROUND_LEASE_CONFIRMATION,
            },
        )
        assert released.status_code == 200, released.text
        assert job["agent_id"] in released.json()["pending_agents"]
        async with session_maker() as session:
            audit = (
                await session.scalars(
                    select(GeneratorOperatorAction).order_by(GeneratorOperatorAction.id)
                )
            ).all()
            assert [(a.action, a.subject, a.actor) for a in audit][1:] == [
                ("release_lease", "round:1", "backroom:test")
            ]
            assert audit[0].action == "register_default_generator"
        relet = (
            await client.post(
                "/api/v1/validator/generator-round-job",
                headers={"X-Validator-Hotkey": _VALIDATOR.ss58_address},
                json=_signed_job(),
            )
        ).json()

        # The round ends with no results: void, and the live lease is expired.
        await _advance(session_maker, 8)
        async with session_maker() as session:
            row = await get_round(session, 1)
            assert row is not None and row.status == "void"
            assert (row.void_reason or "").startswith("insufficient_panel: 0 ")
            ticket = await session.get(
                ValidatorTicket, (UUID(relet["agent_id"]), 13, _VALIDATOR.ss58_address)
            )
            assert ticket is not None and ticket.status == TicketStatus.EXPIRED
            attempts = (
                await session.scalars(
                    select(GeneratorRoundAttempt).where(
                        GeneratorRoundAttempt.round_index == 1
                    )
                )
            ).all()
            assert {a.status for a in attempts} == {"failed"}

    async def test_a_seventy_case_column_round_leases_only_where_it_fits(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """At 70 cases per column a round is 350 cases: it is leased only to a
        validator whose scorer runs 70-case columns, and only while the round
        has the scaled cutoff (~28 minutes) left."""
        chain = _install(app, session_maker)
        await _apply_settings(client, validator_rounds_enabled=True, cases_per_seed=70)
        agents = [await _seed_agent(session_maker, rank) for rank in range(3)]
        _patch_panel(monkeypatch, agents)
        _patch_lane(monkeypatch)
        await _register_default(client)
        await _qualify_all(session_maker)
        await _advance(session_maker, 9)
        await _advance(session_maker, 12)
        work = (
            await client.get("/api/v1/generator-scorer/round-work", headers=_SCORER)
        ).json()["work"]
        assert work["cases_per_column"] == 70 and len(work["columns"]) == 5
        resp = await client.post(
            "/api/v1/generator-scorer/round-results",
            headers=_SCORER,
            json=_generation(work),
        )
        assert resp.status_code == 200, resp.text
        async with session_maker() as session:
            row = await get_round(session, 3)
            assert row is not None and row.open_block is not None
        close_block = row.open_block + row.epochs_per_round * 360
        last_epoch = row.round_index * row.epochs_per_round + row.epochs_per_round - 1

        def _left(minutes: int) -> AsyncMock:
            return AsyncMock(
                return_value=_head(
                    epoch=last_epoch,
                    last_epoch_block=close_block - 360,
                    block=close_block - minutes * 60 // 12,
                )
            )

        async def _request() -> httpx.Response:
            return await client.post(
                "/api/v1/validator/generator-round-job",
                headers={"X-Validator-Hotkey": _VALIDATOR.ss58_address},
                json=_signed_job(),
            )

        # A validator released with the 32-case column gets nothing.
        chain.read_epoch_schedule = _left(60)
        monkeypatch.setattr(lane_mod, "_round_column_capacity", lambda *_a, **_k: 32)
        assert (await _request()).status_code == 204
        monkeypatch.setattr(
            lane_mod,
            "_round_column_capacity",
            lambda *_a, **_k: MAX_ROUND_COLUMN_CASES,
        )
        # 25 minutes clears the 20-minute floor but not this round's cutoff.
        chain.read_epoch_schedule = _left(25)
        assert (await _request()).status_code == 204
        chain.read_epoch_schedule = _left(30)
        job = await _request()
        assert job.status_code == 200, job.text
        assert job.json()["case_count"] == 350

    async def test_a_round_reached_after_its_first_epoch_is_void(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install(app, session_maker)
        await _apply_settings(client)
        agents = [await _seed_agent(session_maker, rank) for rank in range(3)]
        _patch_panel(monkeypatch, agents)
        await _register_default(client)
        await _qualify_all(session_maker)
        await _advance(session_maker, 3)  # commits round 1 (epochs 4..7)
        actions = await _advance(session_maker, 5)  # the loop missed epoch 4
        assert "round 1: void (opening epoch missed)" in actions
        async with session_maker() as session:
            row = await get_round(session, 1)
            assert row is not None and row.status == "void" and row.seed is None
            assert row.void_reason == "round opening epoch was missed"

    async def test_an_interval_change_never_remaps_an_open_round(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install(app, session_maker)
        await _apply_settings(client)
        agents = [await _seed_agent(session_maker, rank) for rank in range(3)]
        _patch_panel(monkeypatch, agents)
        await _register_default(client)
        await _qualify_all(session_maker)
        await _advance(session_maker, 3)  # commits round 1 (epochs 4..7)

        # A committed round never opened, so it is voided with the change.
        await _apply_settings(client, epochs_per_round=5)
        async with session_maker() as session:
            row = await get_round(session, 1)
            assert row is not None and row.status == "void"
            assert row.void_reason == "round interval changed from 4 to 5"

        # The voided index is never reused: under 5 epochs the next round is
        # round 2 (epochs 10..14), and once it opens it blocks the change.
        await _advance(session_maker, 9)  # commits round 2
        await _advance(session_maker, 10)  # opens it
        async with session_maker() as session:
            opened = await get_round(session, 2)
            assert opened is not None
            assert opened.status in ("generating", "running")
        refused = await _apply_settings(client, status=409, epochs_per_round=4)
        assert "are open" in refused.text

    async def test_mode_off_and_missing_generators(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install(app, session_maker)
        assert await _advance(session_maker, 5) == []  # mode off: nothing
        await _apply_settings(client)
        agents = [await _seed_agent(session_maker, rank) for rank in range(3)]
        _patch_panel(monkeypatch, agents)
        preview = (await client.get(f"{_ROUNDS_URL}/preview", headers=_ADMIN)).json()
        assert any("no public default generator" in b for b in preview["blockers"])
        assert any("validator_rounds_enabled" in b for b in preview["blockers"])
        await _advance(session_maker, 5)
        await _advance(session_maker, 8)
        async with session_maker() as session:
            row = await get_round(session, 2)
            assert row is not None and row.status == "void"
            assert "no counted generator columns" in (row.void_reason or "")

        # An unqualified default generator does not fill columns yet.
        await _register_default(client)
        preview = (await client.get(f"{_ROUNDS_URL}/preview", headers=_ADMIN)).json()
        assert any("not qualified yet" in b for b in preview["blockers"])
        assert preview["awaiting_qualification"] == 1
        await _qualify_all(session_maker)
        preview = (await client.get(f"{_ROUNDS_URL}/preview", headers=_ADMIN)).json()
        assert len(preview["columns"]) == 5 and len(preview["panel"]) == 3


class TestPanelFreshness:
    async def test_ineligible_ranked_agents_are_skipped_and_refilled_at_freeze(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install(app, session_maker)
        await _apply_settings(client)
        agents = [await _seed_agent(session_maker, rank) for rank in range(5)]
        _patch_panel(monkeypatch, agents)
        await _register_default(client)
        await _qualify_all(session_maker)
        # Banned after ranking but before the panel freezes: the champion's
        # agent, and a miner whose hotkey is banned.
        await _ban_agent(session_maker, agents[0])
        await _ban_hotkey(session_maker, "5PanelMiner2")

        preview = (await client.get(f"{_ROUNDS_URL}/preview", headers=_ADMIN)).json()
        assert [p["agent_id"] for p in preview["panel"]] == [
            str(agents[i]) for i in (1, 3, 4)
        ]
        assert not any("panel has" in b for b in preview["blockers"])
        assert {s["agent_id"] for s in preview["skipped_panel_agents"]} == {
            str(agents[0]),
            str(agents[2]),
        }

        await _advance(session_maker, 3)
        await _advance(session_maker, 4)
        async with session_maker() as session:
            row = await get_round(session, 1)
            assert row is not None and row.status == "generating"
            panel = row.panel or []
            # Refilled from the next eligible owners, still one per owner.
            assert [slot["agent_id"] for slot in panel] == [
                str(agents[i]) for i in (1, 3, 4)
            ]
            assert len({slot["owner_key"] for slot in panel}) == 3
            # The ledger champion cannot run, so nobody seated is champion.
            assert not any(slot["champion"] for slot in panel)

        detail = (await client.get(f"{_ROUNDS_URL}/1", headers=_ADMIN)).json()
        skipped = {s["agent_id"]: s["reason"] for s in detail["skipped_panel_agents"]}
        assert skipped == {
            str(agents[0]): "agent status is banned",
            str(agents[2]): "miner hotkey is banned",
        }

    async def test_an_undersized_frozen_panel_voids_and_keeps_its_skips(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install(app, session_maker)
        await _apply_settings(client)
        agents = [await _seed_agent(session_maker, rank) for rank in range(3)]
        _patch_panel(monkeypatch, agents)
        await _register_default(client)
        await _qualify_all(session_maker)
        await _ban_agent(session_maker, agents[1])

        await _advance(session_maker, 3)
        await _advance(session_maker, 4)
        detail = (await client.get(f"{_ROUNDS_URL}/1", headers=_ADMIN)).json()
        assert detail["summary"]["status"] == "void"
        assert detail["summary"]["void_reason"].startswith(
            "panel has 2 agent(s), min_panel 3"
        )
        assert detail["skipped_panel_agents"] == [
            {
                "agent_id": str(agents[1]),
                "owner_key": "coldkey:1",
                "miner_hotkey": "5PanelMiner1",
                "reason": "agent status is banned",
            }
        ]

    async def test_agents_banned_mid_round_are_dropped_without_voiding(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install(app, session_maker)
        await _apply_settings(client, panel_size=5, validator_rounds_enabled=True)
        agents = [await _seed_agent(session_maker, rank) for rank in range(6)]
        _patch_panel(monkeypatch, agents)
        _patch_lane(monkeypatch)
        await _register_default(client)
        await _qualify_all(session_maker)
        await _advance(session_maker, 3)
        await _advance(session_maker, 4)
        case_ids = await _generate_round(client)

        # The first agent reports, then is banned: its result must not count.
        first = await _lease_job(client)
        assert first is not None and first["agent_id"] == str(agents[0])
        await _report(client, first, case_ids)
        await _ban_agent(session_maker, agents[0])
        # The lane drops it on its next poll and leases the second agent,
        # whose miner's hotkey is then banned mid-run.
        second = await _lease_job(client)
        assert second is not None and second["agent_id"] == str(agents[1])
        await _ban_hotkey(session_maker, "5PanelMiner1")

        actions = await _advance(session_maker, 5)
        assert any(f"dropped panel agent {agents[1]}" in a for a in actions)
        async with session_maker() as session:
            row = await get_round(session, 1)
            # Three seated agents remain (>= min_panel 3): still running.
            assert row is not None and row.status == "running"
            ticket = await session.get(
                ValidatorTicket, (agents[1], 13, _VALIDATOR.ss58_address)
            )
            assert ticket is not None and ticket.status == TicketStatus.EXPIRED
        # Ticking again changes nothing.
        assert await _advance(session_maker, 5) == []

        # Only the seated agents are leased (the sixth ranked agent never
        # takes a dropped seat), and their reports close the round.
        seen = []
        while (job := await _lease_job(client)) is not None:
            seen.append(job["agent_id"])
            await _report(client, job, case_ids)
        assert seen == [str(agents[i]) for i in (2, 3, 4)]

        detail = (await client.get(f"{_ROUNDS_URL}/1", headers=_ADMIN)).json()
        assert detail["summary"]["status"] == "scoring"
        assert detail["summary"]["panel_size"] == 5
        assert detail["summary"]["panel_dropped"] == 2
        assert detail["summary"]["results"] == 4
        assert detail["summary"]["answered_results"] == 3
        dropped = {
            p["agent_id"]: p["dropped_reason"]
            for p in detail["panel"]
            if p["dropped_reason"]
        }
        assert dropped == {
            str(agents[0]): "agent status is banned",
            str(agents[1]): "miner hotkey is banned",
        }
        excluded = {r["agent_id"]: r["excluded_reason"] for r in detail["results"]}
        assert excluded[str(agents[0])].startswith("agent dropped from the panel")
        assert excluded[str(agents[2])] is None

        # The dropped agent's result never reaches the scored matrix.
        work = (
            await client.get("/api/v1/generator-scorer/round-work", headers=_SCORER)
        ).json()["work"]
        assert work["kind"] == "score"
        assert [a["agent_id"] for a in work["matrix"]["agents"]] == [
            str(agents[i]) for i in (2, 3, 4)
        ]
        listing = (await client.get(_ROUNDS_URL, headers=_ADMIN)).json()
        summary = next(r for r in listing["rounds"] if r["round_index"] == 1)
        assert summary["panel_dropped"] == 2

    async def test_too_few_eligible_agents_left_voids_the_round(
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
        _patch_lane(monkeypatch)
        await _register_default(client)
        await _qualify_all(session_maker)
        await _advance(session_maker, 3)
        await _advance(session_maker, 4)
        await _generate_round(client)
        job = await _lease_job(client)
        assert job is not None and job["agent_id"] == str(agents[0])
        await _ban_hotkey(session_maker, "5PanelMiner0")

        actions = await _advance(session_maker, 5)
        assert "round 1: void after panel drops" in actions
        async with session_maker() as session:
            row = await get_round(session, 1)
            assert row is not None and row.status == "void"
            assert (row.void_reason or "").startswith("insufficient_panel: 0 ")
            assert "1 panel agent(s) dropped" in (row.void_reason or "")
            ticket = await session.get(
                ValidatorTicket, (agents[0], 13, _VALIDATOR.ss58_address)
            )
            assert ticket is not None and ticket.status == TicketStatus.EXPIRED
            attempts = (
                await session.scalars(
                    select(GeneratorRoundAttempt).where(
                        GeneratorRoundAttempt.round_index == 1
                    )
                )
            ).all()
            assert {a.status for a in attempts} == {"failed"}
        assert await _lease_job(client) is None


class TestFaultOnlyRounds:
    async def test_all_faulted_round_is_void_and_never_touches_the_ledger(
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
        _patch_lane(monkeypatch)
        await _register_default(client)
        miner = await _seed_generator(
            session_maker, hotkey="5GenMinerA", owner="coldkey:ga", salt="a"
        )
        await _qualify_all(session_maker)

        # Round 1 scores normally and sets the generator's ledger score.
        await _advance(session_maker, 3)
        await _advance(session_maker, 4)
        case_ids = await _generate_round(client)
        while (job := await _lease_job(client)) is not None:
            await _report(client, job, case_ids)
        work = (
            await client.get("/api/v1/generator-scorer/round-work", headers=_SCORER)
        ).json()["work"]
        resp = await client.post(
            "/api/v1/generator-scorer/round-score",
            headers=_SCORER,
            json=_matrix_score(work),
        )
        assert resp.status_code == 200, resp.text
        before = (await client.get("/api/v1/generator/board")).json()["entries"]
        assert [(e["miner_hotkey"], e["round_index"]) for e in before] == [
            ("5GenMinerA", 1)
        ]
        async with session_maker() as session:
            sub = await session.get(GeneratorSubmission, miner)
            assert sub is not None
            score_before = sub.round_score
            assert score_before is not None and sub.round_scored_index == 1

        # Round 2: every panel run faults every case (infrastructure).
        await _advance(session_maker, 8)
        case_ids = await _generate_round(client)
        while (job := await _lease_job(client)) is not None:
            await _report(client, job, case_ids, fault=True)

        detail = (await client.get(f"{_ROUNDS_URL}/2", headers=_ADMIN)).json()
        assert detail["summary"]["status"] == "void"
        assert detail["summary"]["void_reason"].startswith(
            "insufficient_answered_results: 0 of 3"
        )
        assert detail["summary"]["results"] == 3
        assert detail["summary"]["answered_results"] == 0
        assert {r["excluded_reason"] for r in detail["results"]} == {
            "faulted every case; counts as not reported"
        }
        # Never leased for scoring, never credited.
        assert (
            await client.get("/api/v1/generator-scorer/round-work", headers=_SCORER)
        ).json()["work"] is None
        after = (await client.get("/api/v1/generator/board")).json()["entries"]
        assert after == before
        async with session_maker() as session:
            sub = await session.get(GeneratorSubmission, miner)
            assert sub is not None
            assert sub.round_score == score_before and sub.round_scored_index == 1
            assert not (
                await session.scalars(
                    select(GeneratorRoundCaseVerdict).where(
                        GeneratorRoundCaseVerdict.round_index == 2
                    )
                )
            ).all()


class TestOperatorControls:
    async def test_void_republish_tick_and_loop(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        chain = _install(app, session_maker)
        await _apply_settings(client, onchain_commitment=True)
        agents = [await _seed_agent(session_maker, rank) for rank in range(3)]
        _patch_panel(monkeypatch, agents)
        await _register_default(client)
        await _qualify_all(session_maker)

        assert (await client.get(f"{_ROUNDS_URL}/loop", headers=_ADMIN)).json()[
            "available"
        ] is False
        chain.read_epoch_schedule = AsyncMock(
            return_value=SimpleNamespace(
                subnet_epoch_index=21,
                last_epoch_block=8560,
                tempo=360,
                block=8565,
            )
        )
        chain.get_finalized_block_hash = AsyncMock(return_value=_BLOCK)
        tick = (await client.post(f"{_ROUNDS_URL}/tick", headers=_ADMIN)).json()
        assert tick["error"] is None
        assert "round 6: committed" in tick["actions"]
        assert tick["schedule"]["current_round"] == 5
        assert tick["schedule"]["next_round_epoch"] == 24
        # Platform never publishes the commitment itself: it waits for elected
        # validators, and the loop has no publication step any more.
        async with session_maker() as session:
            row = await get_round(session, 6)
            assert row is not None and row.commitment_publication == "pending"
        assert not hasattr(GeneratorRoundLoop, "publish_commitments")

        # A failed publication is reopened for validators, not published here.
        async with session_maker() as session, session.begin():
            rnd = await get_round(session, 6, for_update=True)
            assert rnd is not None
            rnd.commitment_publication = "failed"
            rnd.commitment_error = "no validator confirmed"
        retry = await client.post(
            f"{_ROUNDS_URL}/6/republish-commitment",
            headers=_ADMIN,
            json={
                "expected_status": "committed",
                "reason": "validators are back",
                "actor": "backroom:test",
                "confirmation": "REPUBLISH GENERATOR ROUND COMMITMENT",
            },
        )
        assert retry.status_code == 200, retry.text
        assert retry.json()["summary"]["commitment_publication"] == "pending"
        assert retry.json()["commitment_error"] is None
        assert retry.json()["commitment_publishers_target"] == 3

        # Void is confirmation- and status-guarded.
        body = {
            "expected_status": "committed",
            "reason": "operator test void",
            "actor": "backroom:test",
            "confirmation": "VOID",
        }
        assert (
            await client.post(f"{_ROUNDS_URL}/6/void", headers=_ADMIN, json=body)
        ).status_code == 409
        body["confirmation"] = void_round_confirmation(6)
        voided = await client.post(f"{_ROUNDS_URL}/6/void", headers=_ADMIN, json=body)
        assert voided.status_code == 200, voided.text
        assert voided.json()["summary"]["status"] == "void"
        assert (
            await client.post(f"{_ROUNDS_URL}/6/void", headers=_ADMIN, json=body)
        ).status_code == 409

        listing = (await client.get(_ROUNDS_URL, headers=_ADMIN)).json()
        assert listing["counts_by_status"]["void"] >= 1
        assert (await client.get(_ROUNDS_URL)).status_code == 401

    async def test_round_tickets_never_charge_attempts_or_touch_live_work(
        self, session_maker: async_sessionmaker[AsyncSession], monkeypatch
    ) -> None:
        async def _allowed(*_a: Any, **_k: Any) -> bool:
            return True

        import ditto.api_server.v13_scorer_cohort as cohort_mod

        monkeypatch.setattr(cohort_mod, "pinned_validator_allowed", _allowed)
        scored = await _seed_agent(session_maker, 0)
        now = datetime.now(UTC)
        async with session_maker() as session, session.begin():
            session.add(
                ValidatorTicket(
                    agent_id=scored,
                    validator_hotkey=_VALIDATOR.ss58_address,
                    slot_id="slot-1",
                    status=TicketStatus.SCORED,
                    purpose=TicketPurpose.CANONICAL_QUORUM,
                    purpose_revision=1,
                    issued_at=now - timedelta(days=1),
                    deadline=now - timedelta(hours=20),
                    bench_version=13,
                    attempt_count=1,
                    manual_retry_grants=2,
                    legacy_completion_allowed=False,
                )
            )
        async with session_maker() as session, session.begin():
            ticket = await tickets_mod.issue_generator_round_ticket(
                session,
                agent_id=scored,
                validator_hotkey=_VALIDATOR.ss58_address,
                now=now,
                ttl=timedelta(minutes=90),
                seed=5,
                dataset_sha256="ef" * 32,
                slot_id="slot-0",
                bench_version=13,
            )
            assert ticket is not None
            assert ticket.purpose == TicketPurpose.GENERATOR_ROUND
            assert (ticket.attempt_count, ticket.manual_retry_grants) == (1, 2)
        async with session_maker() as session, session.begin():
            # The agent's lease is live: no second round lease on any slot.
            again = await tickets_mod.issue_generator_round_ticket(
                session,
                agent_id=scored,
                validator_hotkey=_VALIDATOR.ss58_address,
                now=now,
                ttl=timedelta(minutes=90),
                seed=5,
                dataset_sha256="ef" * 32,
                slot_id="slot-2",
                bench_version=13,
            )
            assert again is None
        evaluating = await _seed_agent(session_maker, 1)
        async with session_maker() as session, session.begin():
            agent = await session.get(Agent, evaluating)
            assert agent is not None
            agent.status = AgentStatus.EVALUATING
        async with session_maker() as session, session.begin():
            # Only finalized agents: an evaluating agent's canonical quorum
            # owns its tickets.
            assert (
                await tickets_mod.issue_generator_round_ticket(
                    session,
                    agent_id=evaluating,
                    validator_hotkey=_VALIDATOR.ss58_address,
                    now=now,
                    ttl=timedelta(minutes=90),
                    seed=5,
                    dataset_sha256="ef" * 32,
                    slot_id="slot-3",
                    bench_version=13,
                )
                is None
            )


class TestRoundRunsNeverSpendScoringAttempts:
    """SN-334: a round run never counts toward canonical scoring retries."""

    @pytest.fixture(autouse=True)
    def _pinned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _allowed(*_a: Any, **_k: Any) -> bool:
            return True

        import ditto.api_server.v13_scorer_cohort as cohort_mod

        monkeypatch.setattr(cohort_mod, "pinned_validator_allowed", _allowed)

    @staticmethod
    async def _lease(
        maker: async_sessionmaker[AsyncSession],
        agent_id: UUID,
        *,
        now: datetime,
        slot_id: str = "slot-0",
    ) -> ValidatorTicket:
        async with maker() as session, session.begin():
            ticket = await tickets_mod.issue_generator_round_ticket(
                session,
                agent_id=agent_id,
                validator_hotkey=_VALIDATOR.ss58_address,
                now=now,
                ttl=timedelta(minutes=90),
                seed=5,
                dataset_sha256="ef" * 32,
                slot_id=slot_id,
                bench_version=13,
            )
            assert ticket is not None
            assert ticket.purpose == TicketPurpose.GENERATOR_ROUND
            return ticket

    @staticmethod
    def _open_circuit(at: datetime) -> ProviderOutageCircuit:
        return ProviderOutageCircuit(
            provider="openrouter",
            state="open",
            epoch=uuid4(),
            opened_at=at,
            retry_at=at + timedelta(minutes=10),
            last_failure_at=at,
            failure_count=3,
            last_error_code="upstream_5xx",
        )

    async def test_round_on_an_agent_with_no_prior_row_leaves_the_budget(
        self, session_maker: async_sessionmaker[AsyncSession]
    ) -> None:
        agent_id = await _seed_agent(session_maker, 0)
        key = (agent_id, 13, _VALIDATOR.ss58_address)
        now = datetime.now(UTC)

        # Created by the round lane: no scoring lease yet.
        ticket = await self._lease(session_maker, agent_id, now=now)
        assert ticket.attempt_count == 0
        assert not tickets_mod.counts_toward_scoring_retry(ticket)

        # Finished by a result, a validator fail, or an operator release: the
        # row reads as no row at all -- not scored, no cooldown, not spent.
        async with session_maker() as session, session.begin():
            row = await session.get(ValidatorTicket, key, with_for_update=True)
            assert row is not None
            tickets_mod.finish_generator_round_ticket(row)
        async with session_maker() as session:
            row = await session.get(ValidatorTicket, key)
            assert row is not None
            assert (row.status, row.attempt_count, row.retry_after) == (
                TicketStatus.EXPIRED,
                0,
                None,
            )
            assert not tickets_mod.ticket_retry_budget_spent(row)
            assert not tickets_mod.counts_toward_scoring_retry(row)

        # The next round re-leases it without charging, and a lapse past the
        # deadline starts no scoring cooldown.
        await self._lease(session_maker, agent_id, now=now)
        async with session_maker() as session, session.begin():
            await tickets_mod.expire_overdue_tickets(
                session, now=now + timedelta(hours=2)
            )
        async with session_maker() as session:
            row = await session.get(ValidatorTicket, key)
            assert row is not None
            assert (row.status, row.attempt_count, row.retry_after) == (
                TicketStatus.EXPIRED,
                0,
                None,
            )

        # Nor does a provider-outage park mint a scoring resume or write
        # scoring failure evidence onto the row.
        later = now + timedelta(hours=3)
        await self._lease(session_maker, agent_id, now=later)
        async with session_maker() as session, session.begin():
            parked = await park_scoring_leases(
                session, circuit=self._open_circuit(later), now=later
            )
            assert parked == 1
        async with session_maker() as session:
            row = await session.get(ValidatorTicket, key)
            assert row is not None
            assert row.status == TicketStatus.EXPIRED
            assert row.attempt_count == 0
            assert row.retry_after is None
            assert row.provider_outage_epoch is None
            assert row.failure_reason is None
            assert not tickets_mod.ticket_retry_budget_spent(row)

    async def test_round_on_an_existing_row_keeps_its_scoring_history(
        self, session_maker: async_sessionmaker[AsyncSession]
    ) -> None:
        agent_id = await _seed_agent(session_maker, 0)
        key = (agent_id, 13, _VALIDATOR.ss58_address)
        now = datetime.now(UTC)
        failed_at = now - timedelta(days=1)
        async with session_maker() as session, session.begin():
            session.add(
                ValidatorTicket(
                    agent_id=agent_id,
                    validator_hotkey=_VALIDATOR.ss58_address,
                    slot_id="slot-1",
                    status=TicketStatus.EXPIRED,
                    purpose=TicketPurpose.CANONICAL_QUORUM,
                    purpose_revision=1,
                    issued_at=failed_at - timedelta(hours=1),
                    deadline=failed_at,
                    bench_version=13,
                    seed=77,
                    attempt_count=1,
                    manual_retry_grants=1,
                    failure_reason="scoring_error",
                    failure_detail="harness_crashed",
                    failed_at=failed_at,
                    legacy_completion_allowed=False,
                )
            )

        ticket = await self._lease(session_maker, agent_id, now=now)
        assert (ticket.attempt_count, ticket.manual_retry_grants) == (1, 1)
        later = now + timedelta(minutes=5)
        async with session_maker() as session, session.begin():
            parked = await park_scoring_leases(
                session, circuit=self._open_circuit(later), now=later
            )
            assert parked == 1
        async with session_maker() as session:
            row = await session.get(ValidatorTicket, key)
            assert row is not None
            # The canonical attempt, grant and failure evidence are untouched.
            assert (row.attempt_count, row.manual_retry_grants) == (1, 1)
            assert row.provider_outage_epoch is None
            assert row.retry_after is None
            assert (row.failure_reason, row.failure_detail) == (
                "scoring_error",
                "harness_crashed",
            )
            # A finished round lease on a row with history still counts.
            assert tickets_mod.counts_toward_scoring_retry(row)

        await self._lease(session_maker, agent_id, now=later + timedelta(minutes=1))
        async with session_maker() as session, session.begin():
            row = await session.get(ValidatorTicket, key, with_for_update=True)
            assert row is not None
            tickets_mod.finish_generator_round_ticket(row)
        async with session_maker() as session:
            row = await session.get(ValidatorTicket, key)
            assert row is not None
            assert row.status == TicketStatus.SCORED
            assert (row.attempt_count, row.manual_retry_grants) == (1, 1)


def test_round_capability_does_not_need_the_private_v13_transport() -> None:
    """A scorer serving rounds is round-capable with private datasets off.

    The private V13 transport stays disabled fleet-wide, so requiring it here
    would leave every validator unable to lease a round.
    """
    from ditto.tests.db.queries.test_benchmark_rollout import _heartbeat

    now = datetime.now(UTC).replace(microsecond=0)
    heartbeat = _heartbeat("5" + "C" * 47, now, versions=[7, 13], protocol_version=18)
    assert heartbeat.capabilities is not None
    heartbeat.capabilities["sandbox_memory_limit"] = True
    scorer = heartbeat.capabilities["scorer_benchmarks"]
    scorer["private_datasets"] = False
    scorer["generator_rounds"] = True
    assert lane_mod._round_column_capacity(heartbeat, now=now) is not None
    scorer["generator_rounds"] = False
    assert lane_mod._round_column_capacity(heartbeat, now=now) is None


def test_round_column_capacity_defaults_to_the_legacy_column() -> None:
    """A scorer that does not advertise its column size runs only the legacy
    32-case column: its validator refuses a job above the old 8 x 32 bound."""
    from ditto.tests.db.queries.test_benchmark_rollout import _heartbeat

    now = datetime.now(UTC).replace(microsecond=0)
    heartbeat = _heartbeat("5" + "C" * 47, now, versions=[7, 13], protocol_version=18)
    assert heartbeat.capabilities is not None
    heartbeat.capabilities["sandbox_memory_limit"] = True
    scorer = heartbeat.capabilities["scorer_benchmarks"]
    scorer["generator_rounds"] = True
    assert lane_mod._round_column_capacity(heartbeat, now=now) == 32
    scorer["generator_round_max_cases"] = 80
    assert lane_mod._round_column_capacity(heartbeat, now=now) == 80


def test_round_lease_cutoff_scales_with_the_round() -> None:
    """Small rounds keep the 20-minute floor; a round of 70 cases per column
    over five columns needs 5 min + 350 x 4 s, about 28 minutes."""
    assert round_lease_cutoff(0) == ROUND_LEASE_CUTOFF
    assert round_lease_cutoff(160) == ROUND_LEASE_CUTOFF  # 32 x 5
    assert round_lease_cutoff(350) == timedelta(minutes=5, seconds=1400)
    assert round_lease_cutoff(400) == timedelta(minutes=5, seconds=1600)
    # The 8 x 80 cap still leaves a one-epoch round (~72 min) a lease window.
    assert round_lease_cutoff(8 * 80) < timedelta(minutes=50)


def _head(
    *, epoch: int, last_epoch_block: int, block: int, next_epoch: int | None = None
) -> EpochSchedule:
    return EpochSchedule(
        netuid=118,
        subnet_epoch_index=epoch,
        last_epoch_block=last_epoch_block,
        pending_epoch_at=0 if next_epoch is None else next_epoch,
        tempo=360,
        blocks_since_last_step=block - last_epoch_block,
        block=block,
        block_hash=_BLOCK,
        block_timestamp=None,
        next_epoch_block=(last_epoch_block + 360 if next_epoch is None else next_epoch),
    )


def test_round_seconds_left_tracks_the_closing_boundary() -> None:
    row = GeneratorRound(round_index=7, epochs_per_round=1, open_block=9_000)
    cutoff = ROUND_LEASE_CUTOFF.total_seconds()
    closing = round_seconds_left(
        row, _head(epoch=7, last_epoch_block=9_000, block=9_300)
    )
    assert closing == 60 * 12
    assert closing < cutoff
    opened = round_seconds_left(
        row, _head(epoch=7, last_epoch_block=9_000, block=9_005)
    )
    assert opened is not None and opened > cutoff
    # An owner-scheduled early epoch closes the round sooner than the tempo.
    early = _head(epoch=7, last_epoch_block=9_000, block=9_005, next_epoch=9_050)
    assert round_seconds_left(row, early) == 45 * 12
    # A multi-epoch round adds a tempo per epoch still to run after the next.
    row = GeneratorRound(round_index=7, epochs_per_round=4, open_block=9_000)
    mid = round_seconds_left(row, _head(epoch=29, last_epoch_block=9_360, block=9_400))
    assert mid == ((9_360 + 360 + 2 * 360) - 9_400) * 12
    # Past the closing epoch no time is left; before the round, no estimate.
    assert round_seconds_left(row, _head(epoch=32, last_epoch_block=0, block=1)) == 0
    assert round_seconds_left(row, _head(epoch=27, last_epoch_block=0, block=1)) is None
