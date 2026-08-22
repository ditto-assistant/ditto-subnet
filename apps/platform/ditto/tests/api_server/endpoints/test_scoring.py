"""Unit tests for :mod:`ditto.api_server.endpoints.scoring`.

Exercises ``GET /scoring/scores`` against a real Postgres with the chain
permit-check mocked. The ledger read + ordering is covered at the query level in
``tests/db/queries/test_scores.py``; here we assert the endpoint's auth gate and
wire shape.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import bittensor
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
)

import ditto.api_server.endpoints.scoring as scoring_mod
from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.continual_retest_settings import ContinualRetestSettings
from ditto.api_server.dependencies import get_chain_client, get_session
from ditto.api_server.middleware.error_envelope import ERROR_CODE_VALIDATOR_AUTH
from ditto.chain.models import NeuronInfo
from ditto.db.models import (
    Agent,
    BenchmarkRollout,
    ContinualRetestSettingsRevision,
    Score,
    ValidatorHeartbeat,
)
from ditto.db.queries.confirmation_scores import (
    ConfirmationSeedScore,
    append_confirmation_scores,
)
from ditto.db.queries.score_ranking import EfficiencyFactorRequesterNotReady
from ditto.db.queries.scores import upsert_score

_KEYPAIR = bittensor.Keypair.create_from_uri("//Alice")
_BOB = bittensor.Keypair.create_from_uri("//Bob")
_VALIDATOR_HOTKEY = _KEYPAIR.ss58_address
_MINER = "5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm"
_MINER_B = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"
_AUTH_HEADER = {"X-Validator-Hotkey": _VALIDATOR_HOTKEY}
# The era the ledger serves. It used to be left implicit: the fixtures took
# ``upsert_score``'s old default of 2 and an empty database resolves its active
# version to the same number, so the two agreed by accident. Neither is true
# now -- the floor refuses a v2 score outright -- so the era is written down,
# and the activated rollout that makes it the answer to "which version is
# authoritative" is planted below.
_BENCH_VERSION = 7


@pytest.fixture(autouse=True)
async def _live_era_has_activated(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Make the ledger read resolve to the era these tests write.

    ``active_bench_version`` falls back to ``DEFAULT_BENCH_VERSION`` (2) when no
    rollout has ever activated, which is a state production left long ago and
    can never return to. Without the activated v6 -> v7 row the endpoint would
    select a version the floor forbids writing, and every ledger assertion here
    would be made against an empty result.
    """
    async with session_maker() as session, session.begin():
        session.add(
            BenchmarkRollout(
                rollout_id=uuid4(),
                from_version=_BENCH_VERSION - 1,
                desired_version=_BENCH_VERSION,
                status="activated",
                cohort_size=5,
                activated_at=datetime(2026, 6, 1, tzinfo=UTC),
            )
        )


def _scorer_capabilities(now: datetime, *, versions: list[int]) -> dict:
    return {
        "screened_images": True,
        "require_screened_image": True,
        "source_build_fallback": False,
        "full_stack_managed": True,
        "stack_updater": True,
        "sandbox_egress_restricted": True,
        "ticket_inference": False,
        "signed_score_quorum": False,
        "executor_isolation": "ephemeral_vm",
        "scorer_benchmarks": {
            "status": "fresh_verified",
            "supported_bench_versions": versions,
            "observed_at": int(now.timestamp()),
            "software_version": "1.0.0",
            "source_revision": "a" * 40,
        },
    }


def _ledger_headers(
    *,
    nonce: UUID | None = None,
    requested_at: datetime | None = None,
    signing_keypair: bittensor.Keypair = _KEYPAIR,
    validator_hotkey: str = _VALIDATOR_HOTKEY,
) -> dict[str, str]:
    nonce = nonce or uuid4()
    requested_at = requested_at or datetime.now(UTC)
    requested = requested_at.astimezone(UTC).isoformat(timespec="microseconds")
    signed = (f"validator-ledger:v1:{validator_hotkey}:{nonce}:{requested}").encode()
    return {
        "X-Validator-Hotkey": validator_hotkey,
        "X-Validator-Ledger-Nonce": str(nonce),
        "X-Validator-Ledger-Requested-At": requested_at.isoformat(),
        "X-Validator-Ledger-Signature": signing_keypair.sign(signed).hex(),
    }


def _install_db(app: FastAPI, maker: async_sessionmaker[AsyncSession]) -> None:
    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _session


def _install_chain(
    app: FastAPI,
    *,
    permitted: bool = True,
    validator_hotkeys: tuple[str, ...] = (_VALIDATOR_HOTKEY,),
) -> None:
    async def _chain() -> MagicMock:
        c = MagicMock()
        c.get_recent_neurons = AsyncMock(
            return_value=[
                NeuronInfo(
                    hotkey=hotkey,
                    coldkey="5GReceiverColdkeyPlaceholderXXXXXXXXXXXXXXXXXXX",
                    uid=index,
                    stake=1000.0,
                    validator_permit=permitted,
                )
                for index, hotkey in enumerate(validator_hotkeys, start=1)
            ]
        )
        return c

    app.dependency_overrides[get_chain_client] = _chain


def test_v9_score_proof_publishes_signature_bound_base_evidence_digest() -> None:
    proof = scoring_mod._score_proof(
        cast(
            Score,
            SimpleNamespace(
                validator_hotkey=_VALIDATOR_HOTKEY,
                run_id="run-v9",
                composite=0.75,
                seed=42,
                bench_version=9,
                signature="ab" * 64,
                details={
                    "ticket_deadline": "2026-08-08T12:00:00+00:00",
                    "transcript_sha256": "cd" * 32,
                    "base_evidence_sha256": "ef" * 32,
                },
            ),
        )
    )
    assert proof.transcript_sha256 == "cd" * 32
    assert proof.base_evidence_sha256 == "ef" * 32


def test_v8_score_proof_omits_v9_base_evidence_field() -> None:
    proof = scoring_mod._score_proof(
        cast(
            Score,
            SimpleNamespace(
                validator_hotkey=_VALIDATOR_HOTKEY,
                run_id="run-v8",
                composite=0.75,
                seed=42,
                bench_version=8,
                signature="ab" * 64,
                details={
                    "ticket_deadline": "2026-08-08T12:00:00+00:00",
                    "transcript_sha256": "cd" * 32,
                },
            ),
        )
    )
    assert proof.base_evidence_sha256 is None


async def _seed_scored(
    maker: async_sessionmaker[AsyncSession],
    *,
    miner: str,
    composite: float,
    status: AgentStatus = AgentStatus.SCORED,
    created_at: datetime | None = None,
    n: int = 20,
) -> None:
    async with maker() as s, s.begin():
        agent = Agent(
            agent_id=uuid4(),
            miner_hotkey=miner,
            name="agent",
            sha256="ab" * 32,
            size_bytes=524288,
            status=status,
            created_at=created_at or datetime.now(UTC),
        )
        s.add(agent)
        await s.flush()
        await upsert_score(
            s,
            agent_id=agent.agent_id,
            validator_hotkey=_VALIDATOR_HOTKEY,
            bench_version=_BENCH_VERSION,
            run_id="run_1",
            seed=42,
            composite=composite,
            tool_mean=composite,
            memory_mean=composite,
            median_ms=500,
            n=n,
            generated_at=datetime(2026, 6, 8, 12, 0, 0, tzinfo=UTC),
            signature="ab" * 64,
            details={
                "ticket_deadline": "2026-06-08T13:00:00+00:00",
                "transcript_sha256": "cd" * 32,
            },
        )


class TestScoringLedger:
    async def test_first_seen_on_the_wire_is_the_lineage_anchor(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """The one field the champion fold anchors on must be the lineage's.

        Every validator folds this payload byte for byte, so this is where the
        rule actually takes effect: a miner that resubmits while tied at the top
        keeps the arrival time it earned, and does not hand its rival the
        incumbency by shipping an improvement. The owner-family resolution behind
        it is covered in ``tests/db/queries/test_scores.py``.

        0.8999 sits inside the crown-anchor improvement floor (the dethrone
        margin). A 0.010 jump is outside that floor and must not keep this
        clock -- that is the planted-low-score case.
        """
        from ditto.db.queries.scores import MIN_ELIGIBLE_CASES

        arrived = datetime(2026, 6, 8, 15, 52, tzinfo=UTC)
        resubmitted = datetime(2026, 6, 8, 21, 20, tzinfo=UTC)
        await _seed_scored(
            session_maker,
            miner=_MINER,
            composite=0.8999,
            created_at=arrived,
            n=MIN_ELIGIBLE_CASES,
        )
        await _seed_scored(
            session_maker,
            miner=_MINER,
            composite=0.900,
            created_at=resubmitted,
            n=MIN_ELIGIBLE_CASES,
        )
        _install_db(app, session_maker)
        _install_chain(app)

        resp = await client.get("/api/v1/scoring/scores", headers=_ledger_headers())

        assert resp.status_code == 200
        (entry,) = resp.json()["entries"]
        assert entry["composite"] == pytest.approx(0.900)
        assert datetime.fromisoformat(entry["first_seen"]) == arrived

    async def test_a_sub_dethrone_improvement_keeps_the_lineage_anchor_on_the_wire(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """Validators must see the earlier clock for a 0.004 official gain.

        0.889 -> 0.893 is inside KOTH_MARGIN. The fold reads ``first_seen``
        byte for byte, so this is the payload that would otherwise hand a
        lower-scoring intervening rival the incumbency.
        """
        from ditto.db.queries.scores import MIN_ELIGIBLE_CASES

        arrived = datetime(2026, 8, 22, 3, 7, tzinfo=UTC)
        resubmitted = datetime(2026, 8, 22, 13, 50, tzinfo=UTC)
        await _seed_scored(
            session_maker,
            miner=_MINER,
            composite=0.889,
            created_at=arrived,
            n=MIN_ELIGIBLE_CASES,
        )
        await _seed_scored(
            session_maker,
            miner=_MINER,
            composite=0.893,
            created_at=resubmitted,
            n=MIN_ELIGIBLE_CASES,
        )
        _install_db(app, session_maker)
        _install_chain(app)

        resp = await client.get("/api/v1/scoring/scores", headers=_ledger_headers())

        assert resp.status_code == 200
        (entry,) = resp.json()["entries"]
        assert entry["composite"] == pytest.approx(0.893)
        assert datetime.fromisoformat(entry["first_seen"]) == arrived

    async def test_a_two_step_jump_resets_the_lineage_anchor_on_the_wire(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """A generation that never held this score cannot plant its timestamp.

        0.890 is 0.010 behind 0.900 -- outside the 0.007 dethrone-margin
        improvement floor. The later winner keeps its own arrival, so an
        early-but-behind ancestor cannot steal a rival's incumbency.
        """
        from ditto.db.queries.scores import MIN_ELIGIBLE_CASES

        arrived = datetime(2026, 6, 8, 15, 52, tzinfo=UTC)
        resubmitted = datetime(2026, 6, 8, 21, 20, tzinfo=UTC)
        await _seed_scored(
            session_maker,
            miner=_MINER,
            composite=0.890,
            created_at=arrived,
            n=MIN_ELIGIBLE_CASES,
        )
        await _seed_scored(
            session_maker,
            miner=_MINER,
            composite=0.900,
            created_at=resubmitted,
            n=MIN_ELIGIBLE_CASES,
        )
        _install_db(app, session_maker)
        _install_chain(app)

        resp = await client.get("/api/v1/scoring/scores", headers=_ledger_headers())

        assert resp.status_code == 200
        (entry,) = resp.json()["entries"]
        assert entry["composite"] == pytest.approx(0.900)
        assert datetime.fromisoformat(entry["first_seen"]) == resubmitted

    async def test_returns_best_per_miner_highest_first(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_scored(session_maker, miner=_MINER, composite=0.4)
        await _seed_scored(session_maker, miner=_MINER_B, composite=0.9)
        # A held agent must not surface in the eligible ledger.
        await _seed_scored(
            session_maker,
            miner="5HeldMinerXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX",
            composite=0.99,
            status=AgentStatus.ATH_PENDING_REVIEW,
        )
        _install_db(app, session_maker)
        _install_chain(app)

        resp = await client.get("/api/v1/scoring/scores", headers=_ledger_headers())
        assert resp.status_code == 200
        assert resp.headers["Cache-Control"] == "no-store"
        body = resp.json()
        assert body["count"] == 2
        assert body["active_bench_version"] == _BENCH_VERSION
        assert [e["miner_hotkey"] for e in body["entries"]] == [_MINER_B, _MINER]
        assert body["entries"][0]["composite"] == pytest.approx(0.9)
        assert body["entries"][0]["signature"] == "ab" * 64
        assert body["entries"][0]["bench_version"] == _BENCH_VERSION
        # No fleet capability evidence means the additive contract fails closed.
        assert body["entries"][0]["continual_aggregate_method"] is None
        assert body["entries"][0]["score_proofs"] == [
            {
                "validator_hotkey": _VALIDATOR_HOTKEY,
                "run_id": "run_1",
                "composite": 0.9,
                "seed": 42,
                "bench_version": _BENCH_VERSION,
                "ticket_deadline": "2026-06-08T13:00:00Z",
                "transcript_sha256": "cd" * 32,
                "signature": "ab" * 64,
            }
        ]
        # n rides the wire so the validator's eligibility floor can bite (a run
        # below MIN_ELIGIBLE_CASES is dropped from the fold rather than shadowing
        # a real full run.
        assert body["entries"][0]["n"] == 20
        assert len(body["entries"][0]["score_proofs"]) == 1

    async def test_absent_heartbeat_does_not_block_ledger_without_factor_candidates(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_scored(session_maker, miner=_MINER, composite=0.8)
        _install_db(app, session_maker)
        _install_chain(app)

        response = await client.get("/api/v1/scoring/scores", headers=_ledger_headers())

        assert response.status_code == 200
        assert response.json()["entries"][0]["efficiency_factor"] is None

    async def test_stale_requester_is_428_when_factor_candidates_exist(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await _seed_scored(session_maker, miner=_MINER, composite=0.8)
        stale = datetime.now(UTC) - timedelta(hours=1)
        async with session_maker() as session, session.begin():
            session.add(
                ValidatorHeartbeat(
                    validator_hotkey=_VALIDATOR_HOTKEY,
                    software_version="0.28.0",
                    protocol_version=18,
                    code_digest="ab" * 32,
                    state="idle",
                    reported_at=stale,
                    seen_at=stale,
                    signature="cd" * 64,
                )
            )

        resolver = AsyncMock(
            side_effect=EfficiencyFactorRequesterNotReady(
                "a fresh validator heartbeat is required before serving "
                "bounded efficiency factors"
            )
        )
        monkeypatch.setattr(scoring_mod, "resolve_efficiency_adjustments", resolver)
        _install_db(app, session_maker)
        _install_chain(app)

        response = await client.get("/api/v1/scoring/scores", headers=_ledger_headers())

        assert response.status_code == 428
        assert response.json()["message"] == (
            "a fresh validator heartbeat is required before serving "
            "bounded efficiency factors"
        )
        assert resolver.await_args is not None
        assert resolver.await_args.kwargs["requesting_validator_hotkey"] == (
            _VALIDATOR_HOTKEY
        )

    async def test_fresh_protocol_18_requester_receives_factor_neutral_ledger(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await _seed_scored(session_maker, miner=_MINER, composite=0.8)
        now = datetime.now(UTC)
        async with session_maker() as session, session.begin():
            session.add(
                ValidatorHeartbeat(
                    validator_hotkey=_VALIDATOR_HOTKEY,
                    software_version="0.28.0",
                    protocol_version=18,
                    code_digest="ab" * 32,
                    state="idle",
                    reported_at=now,
                    seen_at=now,
                    signature="cd" * 64,
                )
            )

        # The shared resolver owns requester/global readiness. Its neutral result
        # represents the broad fleet minimum seeing this fresh protocol-18 row.
        resolver = AsyncMock(return_value=({}, {}, {}))
        monkeypatch.setattr(scoring_mod, "resolve_efficiency_adjustments", resolver)
        _install_db(app, session_maker)
        _install_chain(app)

        response = await client.get("/api/v1/scoring/scores", headers=_ledger_headers())

        assert response.status_code == 200
        assert response.json()["entries"][0]["efficiency_factor"] is None
        assert resolver.await_args is not None
        assert resolver.await_args.kwargs["requesting_validator_hotkey"] == (
            _VALIDATOR_HOTKEY
        )

    async def test_continual_mean_activates_globally_only_for_protocol_14_fleet(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_scored(session_maker, miner=_MINER, composite=0.9)
        now = datetime.now(UTC)
        async with session_maker() as session, session.begin():
            session.add_all(
                [
                    ValidatorHeartbeat(
                        validator_hotkey=_VALIDATOR_HOTKEY,
                        software_version="0.28.0",
                        protocol_version=14,
                        code_digest="ab" * 32,
                        state="idle",
                        reported_at=now,
                        seen_at=now,
                        signature="cd" * 64,
                        capabilities=_scorer_capabilities(
                            now, versions=[_BENCH_VERSION]
                        ),
                    ),
                    ValidatorHeartbeat(
                        validator_hotkey=_MINER_B,
                        software_version="0.27.0",
                        protocol_version=13,
                        code_digest="ef" * 32,
                        state="idle",
                        reported_at=now,
                        seen_at=now,
                        signature="12" * 64,
                        capabilities=_scorer_capabilities(
                            now, versions=[_BENCH_VERSION]
                        ),
                    ),
                ]
            )
        _install_db(app, session_maker)
        _install_chain(app)

        mixed = await client.get("/api/v1/scoring/scores", headers=_ledger_headers())
        assert mixed.status_code == 200
        mixed_entry = mixed.json()["entries"][0]
        assert mixed_entry["continual_aggregate_method"] is None
        assert mixed_entry["confirmation_composites"] is None
        assert mixed_entry["confirmation_seeds"] is None
        assert mixed_entry["confirmation_history"] is None

        async with session_maker() as session, session.begin():
            legacy = await session.get(ValidatorHeartbeat, _MINER_B)
            assert legacy is not None
            legacy.protocol_version = 14
            legacy.software_version = "0.28.0"

        ready = await client.get("/api/v1/scoring/scores", headers=_ledger_headers())
        assert ready.status_code == 200
        assert (
            ready.json()["entries"][0]["continual_aggregate_method"]
            == "mean_after_quorum"
        )

    async def test_tie_weighting_marker_requires_protocol_20_fleet(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_scored(session_maker, miner=_MINER, composite=0.9)
        now = datetime.now(UTC)
        settings = ContinualRetestSettings(tie_weighting_mode="fleet_ready").model_dump(
            mode="json"
        )
        async with session_maker() as session, session.begin():
            session.add(
                ContinualRetestSettingsRevision(
                    parent_revision=0,
                    scope="*",
                    settings=settings,
                    checksum="ab" * 32,
                    reason="activate tie-aware weight pooling",
                    actor="operator@example.com",
                )
            )
            session.add(
                ValidatorHeartbeat(
                    validator_hotkey=_VALIDATOR_HOTKEY,
                    software_version="0.55.0",
                    protocol_version=19,
                    code_digest="ab" * 32,
                    state="idle",
                    reported_at=now,
                    seen_at=now,
                    signature="cd" * 64,
                    capabilities=_scorer_capabilities(now, versions=[_BENCH_VERSION]),
                )
            )
        _install_db(app, session_maker)
        _install_chain(app)
        app.state.session_maker = session_maker
        app.state.continual_retest_settings.invalidate()

        mixed = await client.get("/api/v1/scoring/scores", headers=_ledger_headers())
        assert mixed.status_code == 200
        assert mixed.json().get("tie_weighting_mode") is None

        async with session_maker() as session, session.begin():
            heartbeat = await session.get(ValidatorHeartbeat, _VALIDATOR_HOTKEY)
            assert heartbeat is not None
            heartbeat.protocol_version = 20
        app.state.continual_retest_settings.invalidate()

        ready = await client.get("/api/v1/scoring/scores", headers=_ledger_headers())
        assert ready.status_code == 200
        assert ready.json()["tie_weighting_mode"] == "pool"

    async def test_empty_ledger(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        _install_chain(app)
        resp = await client.get("/api/v1/scoring/scores", headers=_ledger_headers())
        assert resp.status_code == 200
        body = resp.json()
        assert body["entries"] == []
        assert body["count"] == 0
        assert body["active_bench_version"] == _BENCH_VERSION
        # A fresh read is never stale.
        assert body["stale"] is False
        assert body["age_seconds"] == 0
        assert body["generated_at"] is not None

    async def test_missing_auth_returns_401(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        _install_chain(app)
        resp = await client.get("/api/v1/scoring/scores")
        assert resp.status_code == 401
        assert resp.json()["error_code"] == ERROR_CODE_VALIDATOR_AUTH

    async def test_public_validator_identity_without_signature_returns_401(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        _install_chain(app)
        resp = await client.get("/api/v1/scoring/scores", headers=_AUTH_HEADER)
        assert resp.status_code == 401
        assert resp.json()["error_code"] == ERROR_CODE_VALIDATOR_AUTH

    async def test_malformed_validator_hotkey_returns_401(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        _install_chain(app)
        headers = _ledger_headers()
        headers["X-Validator-Hotkey"] = "not-an-ss58-hotkey"
        resp = await client.get("/api/v1/scoring/scores", headers=headers)
        assert resp.status_code == 401
        assert resp.json()["error_code"] == ERROR_CODE_VALIDATOR_AUTH

    async def test_replayed_ledger_proof_returns_409(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        _install_chain(app)
        headers = _ledger_headers()
        first = await client.get("/api/v1/scoring/scores", headers=headers)
        replay = await client.get("/api/v1/scoring/scores", headers=headers)
        assert first.status_code == 200
        assert replay.status_code == 409

    # The offset, not the timestamp. Parametrize arguments are evaluated once
    # at collection time, so a literal `datetime.now(UTC) + timedelta(minutes=3)`
    # here is three minutes past *collection*, and the endpoint's window
    # (`_LEDGER_REQUEST_MAX_AGE`) is two minutes wide. Any run that reached this
    # test more than a minute after collection -- i.e. the full suite, always --
    # saw the future case drift back inside the window and get a 200. The stale
    # case only ever got staler, which is why the bug presented as one
    # perpetually-red parametrization rather than two.
    @pytest.mark.parametrize(
        "skew",
        [timedelta(minutes=-3), timedelta(minutes=3)],
        ids=["stale", "too-far-in-future"],
    )
    async def test_out_of_window_ledger_proof_returns_409(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        skew: timedelta,
    ) -> None:
        _install_db(app, session_maker)
        _install_chain(app)
        resp = await client.get(
            "/api/v1/scoring/scores",
            headers=_ledger_headers(requested_at=datetime.now(UTC) + skew),
        )
        assert resp.status_code == 409

    async def test_ledger_proof_signed_by_different_key_returns_401(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        _install_chain(app)
        resp = await client.get(
            "/api/v1/scoring/scores",
            headers=_ledger_headers(
                signing_keypair=bittensor.Keypair.create_from_uri("//Bob")
            ),
        )
        assert resp.status_code == 401
        assert resp.json()["error_code"] == ERROR_CODE_VALIDATOR_AUTH

    async def test_unpermitted_returns_401(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        _install_chain(app, permitted=False)
        resp = await client.get("/api/v1/scoring/scores", headers=_ledger_headers())
        assert resp.status_code == 401
        assert resp.json()["error_code"] == ERROR_CODE_VALIDATOR_AUTH


class TestScoringLiveness:
    """Serve-last-known + staleness policy on a transient DB failure."""

    @staticmethod
    def _break_db(monkeypatch: pytest.MonkeyPatch) -> None:
        async def _boom(_session: object, **_kwargs: object) -> list:
            raise OperationalError("SELECT ...", {}, Exception("db down"))

        monkeypatch.setattr(scoring_mod, "list_eligible_ledger", _boom)

    async def test_db_failure_with_no_cache_returns_503(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_db(app, session_maker)
        _install_chain(app)
        self._break_db(monkeypatch)
        resp = await client.get("/api/v1/scoring/scores", headers=_ledger_headers())
        assert resp.status_code == 503

    async def test_reads_share_one_bounded_fresh_materialization(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await _seed_scored(session_maker, miner=_MINER, composite=0.7)
        _install_db(app, session_maker)
        _install_chain(app)

        real_list = scoring_mod.list_eligible_ledger
        first_read_started = asyncio.Event()
        release_first_read = asyncio.Event()
        calls = 0

        async def _gated_list(*args: Any, **kwargs: Any) -> Any:
            nonlocal calls
            calls += 1
            if calls == 1:
                first_read_started.set()
                await release_first_read.wait()
            return await real_list(*args, **kwargs)

        monkeypatch.setattr(scoring_mod, "list_eligible_ledger", _gated_list)
        first = asyncio.create_task(
            client.get("/api/v1/scoring/scores", headers=_ledger_headers())
        )
        await asyncio.wait_for(first_read_started.wait(), timeout=2)
        second = asyncio.create_task(
            client.get("/api/v1/scoring/scores", headers=_ledger_headers())
        )
        await asyncio.sleep(0.05)
        assert calls == 1

        release_first_read.set()
        first_response, second_response = await asyncio.gather(first, second)
        assert first_response.status_code == 200
        assert second_response.status_code == 200
        assert calls == 1
        assert second_response.json() == first_response.json()

        # A later non-overlapping read in the same validator sweep reuses the
        # successful snapshot instead of rebuilding the full ledger.
        third_response = await client.get(
            "/api/v1/scoring/scores", headers=_ledger_headers()
        )
        assert third_response.status_code == 200
        assert third_response.json() == first_response.json()
        assert calls == 1

        # The cache is bounded. Once the materialization ages past one sweep,
        # the next authenticated request owns a new live read.
        app.state.ledger_snapshot.generated_at -= timedelta(
            seconds=scoring_mod._FRESH_SNAPSHOT_SECONDS + 1
        )
        fourth_response = await client.get(
            "/api/v1/scoring/scores", headers=_ledger_headers()
        )
        assert fourth_response.status_code == 200
        assert calls == 2

    async def test_dynamic_policy_change_invalidates_fresh_snapshot(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await _seed_scored(session_maker, miner=_MINER, composite=0.7)
        _install_db(app, session_maker)
        _install_chain(app)

        real_list = scoring_mod.list_eligible_ledger
        calls = 0

        async def _counted_list(*args: Any, **kwargs: Any) -> Any:
            nonlocal calls
            calls += 1
            return await real_list(*args, **kwargs)

        initial = app.state.config.efficiency_bonus
        resolve = AsyncMock(return_value=initial)
        monkeypatch.setattr(app.state.efficiency_settings, "resolve", resolve)
        monkeypatch.setattr(scoring_mod, "list_eligible_ledger", _counted_list)

        first = await client.get("/api/v1/scoring/scores", headers=_ledger_headers())
        assert first.status_code == 200
        assert calls == 1

        resolve.return_value = replace(initial, cap=initial.cap + 0.01)
        second = await client.get("/api/v1/scoring/scores", headers=_ledger_headers())
        assert second.status_code == 200
        assert calls == 2

    async def test_factor_ready_validators_share_inflight_materialization(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await _seed_scored(session_maker, miner=_MINER, composite=0.7)
        now = datetime.now(UTC)
        async with session_maker() as session, session.begin():
            session.add_all(
                [
                    ValidatorHeartbeat(
                        validator_hotkey=keypair.ss58_address,
                        software_version="0.28.0",
                        protocol_version=21,
                        code_digest="ab" * 32,
                        state="idle",
                        reported_at=now,
                        seen_at=now,
                        signature="cd" * 64,
                        capabilities=_scorer_capabilities(
                            now, versions=[_BENCH_VERSION]
                        ),
                    )
                    for keypair in (_KEYPAIR, _BOB)
                ]
            )
        _install_db(app, session_maker)
        app.state.session_maker = session_maker
        _install_chain(
            app,
            validator_hotkeys=(_VALIDATOR_HOTKEY, _BOB.ss58_address),
        )

        real_list = scoring_mod.list_eligible_ledger
        first_read_started = asyncio.Event()
        release_first_read = asyncio.Event()
        calls = 0

        async def _gated_list(*args: Any, **kwargs: Any) -> Any:
            nonlocal calls
            calls += 1
            if calls == 1:
                first_read_started.set()
                await release_first_read.wait()
            return await real_list(*args, **kwargs)

        async def _factor_adjustments(*_args: Any, **kwargs: Any) -> Any:
            factors = {row.agent_id: 1.05 for row in kwargs["rows"]}
            return {}, factors, dict.fromkeys(factors, 3)

        def _stable_tiebreaks(
            rows: Any, *, official: Any, efficiency_factors: Any, **_kwargs: Any
        ) -> Any:
            assert efficiency_factors
            return {row.agent_id: official[row.agent_id] for row in rows}

        monkeypatch.setattr(scoring_mod, "list_eligible_ledger", _gated_list)
        monkeypatch.setattr(
            scoring_mod, "resolve_efficiency_adjustments", _factor_adjustments
        )
        monkeypatch.setattr(
            scoring_mod,
            "efficiency_tiebreak_composites",
            _stable_tiebreaks,
        )
        first = asyncio.create_task(
            client.get("/api/v1/scoring/scores", headers=_ledger_headers())
        )
        await asyncio.wait_for(first_read_started.wait(), timeout=2)
        second = asyncio.create_task(
            client.get(
                "/api/v1/scoring/scores",
                headers=_ledger_headers(
                    signing_keypair=_BOB,
                    validator_hotkey=_BOB.ss58_address,
                ),
            )
        )
        await asyncio.sleep(0.05)
        assert calls == 1

        release_first_read.set()
        first_response, second_response = await asyncio.gather(first, second)
        assert first_response.status_code == 200, first_response.text
        assert second_response.status_code == 200, second_response.text
        assert calls == 1
        assert first_response.json()["entries"][0]["efficiency_factor"] == 1.05
        assert second_response.json() == first_response.json()

        # A waiter outside the factor-capable protocol cohort must still take
        # the normal materialization path, where the resolver can return its
        # neutral or fail-closed response instead of inheriting these factors.
        async with session_maker() as session, session.begin():
            bob = await session.get(ValidatorHeartbeat, _BOB.ss58_address)
            assert bob is not None
            bob.protocol_version = 20
        assert not await scoring_mod._snapshot_can_be_shared(
            cast(Any, SimpleNamespace(app=app)),
            app.state.ledger_snapshot,
            _BOB.ss58_address,
            now=datetime.now(UTC),
        )

    async def test_nonce_db_failure_returns_503_without_serving_cache(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await _seed_scored(session_maker, miner=_MINER, composite=0.7)
        _install_db(app, session_maker)
        _install_chain(app)

        # Prime a snapshot that the normal ledger-read fallback could serve.
        ok = await client.get("/api/v1/scoring/scores", headers=_ledger_headers())
        assert ok.status_code == 200

        async def _break_nonce(*_args: object, **_kwargs: object) -> None:
            raise OperationalError("INSERT ...", {}, Exception("db down"))

        monkeypatch.setattr(scoring_mod, "consume_validator_nonce", _break_nonce)
        resp = await client.get("/api/v1/scoring/scores", headers=_ledger_headers())
        assert resp.status_code == 503
        assert resp.json()["message"] == (
            "scoring ledger authorization temporarily unavailable"
        )

    async def test_db_failure_serves_last_known_good(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await _seed_scored(session_maker, miner=_MINER, composite=0.7)
        _install_db(app, session_maker)
        _install_chain(app)

        # First read succeeds and caches the snapshot.
        ok = await client.get("/api/v1/scoring/scores", headers=_ledger_headers())
        assert ok.status_code == 200
        assert ok.json()["stale"] is False

        # A later read fails: the cached ledger is served, flagged stale.
        app.state.ledger_snapshot.generated_at -= timedelta(
            seconds=scoring_mod._FRESH_SNAPSHOT_SECONDS + 1
        )
        self._break_db(monkeypatch)
        stale = await client.get("/api/v1/scoring/scores", headers=_ledger_headers())
        assert stale.status_code == 200
        body = stale.json()
        assert body["stale"] is True
        assert body["active_bench_version"] == _BENCH_VERSION
        assert body["count"] == 1
        assert body["entries"][0]["miner_hotkey"] == _MINER
        assert body["age_seconds"] >= 0

    async def test_cache_too_stale_returns_503(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await _seed_scored(session_maker, miner=_MINER, composite=0.7)
        _install_db(app, session_maker)
        _install_chain(app)

        # Prime the cache, then age it past the staleness limit.
        assert (
            await client.get("/api/v1/scoring/scores", headers=_ledger_headers())
        ).status_code == 200
        snap = app.state.ledger_snapshot
        snap.generated_at = snap.generated_at - timedelta(
            seconds=scoring_mod._MAX_STALE_SECONDS + 60
        )

        self._break_db(monkeypatch)
        resp = await client.get("/api/v1/scoring/scores", headers=_ledger_headers())
        assert resp.status_code == 503
        assert "staleness limit" in resp.json()["message"]


def test_composite_stderr_reads_details() -> None:
    """The ledger surfaces composite_stderr from the score details blob, and
    degrades to None for absent or malformed values (flat-margin fold)."""
    from ditto.api_server.endpoints.scoring import _composite_stderr

    assert _composite_stderr({"composite_stderr": 0.031}) == 0.031
    assert _composite_stderr({"composite_stderr": 0}) == 0.0
    assert _composite_stderr({}) is None
    assert _composite_stderr(None) is None
    assert _composite_stderr({"composite_stderr": -1.0}) is None
    assert _composite_stderr({"composite_stderr": "0.5"}) is None
    assert _composite_stderr({"composite_stderr": True}) is None
    assert _composite_stderr({"composite_stderr": float("inf")}) is None


def test_confirmation_composites_reads_details() -> None:
    """The ledger surfaces the P4 per-seed confirmation composites from the score
    details blob, and degrades to None for absent or malformed values so the
    validator's fold falls back to the raw composite."""
    from ditto.api_server.endpoints.scoring import _confirmation_composites

    assert _confirmation_composites({"confirmation_composites": [0.7, 0.8, 0.9]}) == [
        0.7,
        0.8,
        0.9,
    ]
    assert _confirmation_composites({"confirmation_composites": [0.5]}) == [0.5]
    assert _confirmation_composites({}) is None
    assert _confirmation_composites(None) is None
    assert _confirmation_composites({"confirmation_composites": []}) is None
    assert _confirmation_composites({"confirmation_composites": "x"}) is None
    # Any out-of-range, non-numeric, boolean, or non-finite element voids the list.
    assert _confirmation_composites({"confirmation_composites": [0.5, 1.5]}) is None
    assert _confirmation_composites({"confirmation_composites": [0.5, -0.1]}) is None
    assert _confirmation_composites({"confirmation_composites": [0.5, "0.6"]}) is None
    assert _confirmation_composites({"confirmation_composites": [0.5, True]}) is None
    assert (
        _confirmation_composites({"confirmation_composites": [0.5, float("nan")]})
        is None
    )


def test_confirmation_seeds_reads_details() -> None:
    """The ledger surfaces the P4 confirmation CRN seeds (aligned 1:1 with the
    composites) from the score details blob, and degrades to None for absent or
    malformed values so the validator's fold falls back to the unpaired band."""
    from ditto.api_server.endpoints.scoring import _confirmation_seeds

    assert _confirmation_seeds({"confirmation_seeds": [10, 20, 30]}) == [10, 20, 30]
    assert _confirmation_seeds({"confirmation_seeds": [0]}) == [0]
    assert _confirmation_seeds({}) is None
    assert _confirmation_seeds(None) is None
    assert _confirmation_seeds({"confirmation_seeds": []}) is None
    assert _confirmation_seeds({"confirmation_seeds": "x"}) is None
    # Any negative, non-int, boolean, or float element voids the list.
    assert _confirmation_seeds({"confirmation_seeds": [10, -1]}) is None
    assert _confirmation_seeds({"confirmation_seeds": [10, 2.5]}) is None
    assert _confirmation_seeds({"confirmation_seeds": [10, "20"]}) is None
    assert _confirmation_seeds({"confirmation_seeds": [10, True]}) is None


def test_quorum_stderr_is_between_validator_sem() -> None:
    """The quorum SEM = stdev(composites) / sqrt(n); < 2 scores -> None; a
    degenerate (identical) quorum -> 0.0 (band collapses to the flat margin)."""
    from ditto.api_server.endpoints.scoring import _quorum_stderr

    # stdev([0.80, 0.85, 0.90]) = 0.05, SEM = 0.05 / sqrt(3).
    assert _quorum_stderr([0.80, 0.85, 0.90]) == pytest.approx(0.05 / 3**0.5)
    assert _quorum_stderr([0.8, 0.8, 0.8]) == pytest.approx(0.0, abs=1e-12)
    assert _quorum_stderr([0.8]) is None
    assert _quorum_stderr([]) is None
    # Non-finite scores are dropped before the SEM.
    assert _quorum_stderr([0.8, float("nan")]) is None


def test_ledger_stderr_prefers_stashed_then_quorum() -> None:
    """The ledger SE prefers a run's own stashed composite_stderr (e.g. a
    confirmation re-score's pooled SE); otherwise it falls back to the quorum
    SEM; None only when neither is available."""
    from ditto.api_server.endpoints.scoring import _ledger_stderr

    # Stashed present -> used verbatim, quorum ignored.
    assert _ledger_stderr({"composite_stderr": 0.012}, [0.7, 0.9]) == pytest.approx(
        0.012
    )
    # No stash -> quorum SEM.
    assert _ledger_stderr(None, [0.80, 0.85, 0.90]) == pytest.approx(0.05 / 3**0.5)
    # stdev([0.80, 0.90]) = 0.1/sqrt(2); SEM divides by sqrt(2) again -> 0.05.
    assert _ledger_stderr({}, [0.80, 0.90]) == pytest.approx(0.05)
    # Neither -> None (band stays inert / flat margin).
    assert _ledger_stderr(None, [0.8]) is None
    assert _ledger_stderr(None, []) is None


class TestScoringLedgerConfirmationHistory:
    async def test_exposes_raw_append_only_confirmation_records(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        from datetime import UTC, datetime
        from uuid import uuid4

        aid = uuid4()
        async with session_maker() as s, s.begin():
            now = datetime.now(UTC)
            s.add(
                ValidatorHeartbeat(
                    validator_hotkey=_VALIDATOR_HOTKEY,
                    software_version="0.28.0",
                    protocol_version=14,
                    code_digest="ab" * 32,
                    state="idle",
                    reported_at=now,
                    seen_at=now,
                    signature="cd" * 64,
                    capabilities=_scorer_capabilities(now, versions=[_BENCH_VERSION]),
                )
            )
            s.add(
                Agent(
                    agent_id=aid,
                    miner_hotkey=_MINER,
                    name="agent",
                    sha256="ab" * 32,
                    size_bytes=524288,
                    status=AgentStatus.SCORED,
                    created_at=datetime.now(UTC),
                )
            )
            await s.flush()
            await upsert_score(
                s,
                agent_id=aid,
                validator_hotkey=_VALIDATOR_HOTKEY,
                bench_version=_BENCH_VERSION,
                run_id="run_1",
                seed=42,
                composite=0.9,
                tool_mean=0.9,
                memory_mean=0.9,
                median_ms=500,
                n=20,
                generated_at=datetime(2026, 6, 8, tzinfo=UTC),
                signature="ab" * 64,
            )
            # Two validators on seed 100, one on seed 200, all on the active era.
            await append_confirmation_scores(
                s,
                rows=[
                    ConfirmationSeedScore(aid, "5V1", 100, 0.80, "r1", "ab" * 64),
                    ConfirmationSeedScore(aid, "5V2", 100, 0.84, "r2", "cd" * 64),
                    ConfirmationSeedScore(aid, "5V1", 200, 0.70, "r3", None),
                ],
                bench_version=_BENCH_VERSION,
                created_at=datetime.now(UTC),
            )
        _install_db(app, session_maker)
        _install_chain(app)

        resp = await client.get("/api/v1/scoring/scores", headers=_ledger_headers())
        assert resp.status_code == 200
        entry = resp.json()["entries"][0]
        history = entry["confirmation_history"]
        # Raw per-(validator, seed) records, NOT pre-aggregated (3 rows, not 2).
        assert len(history) == 3
        assert {(h["seed"], h["validator_hotkey"]) for h in history} == {
            (100, "5V1"),
            (100, "5V2"),
            (200, "5V1"),
        }
        assert {h["bench_version"] for h in history} == {_BENCH_VERSION}


class TestLedgerBurnShare:
    """The operator-owned miner/burn split rides the ledger to the fleet."""

    @staticmethod
    async def _set_burn(
        app: FastAPI, maker: async_sessionmaker[AsyncSession], share: float
    ) -> None:
        from ditto.db.models import BurnSettingsRevision

        async with maker() as session, session.begin():
            session.add(
                BurnSettingsRevision(
                    parent_revision=0,
                    scope="*",
                    settings={"burn_share": share},
                    checksum="ab" * 32,
                    reason="operator burn policy for the test",
                    actor="operator@example.com",
                )
            )
        app.state.session_maker = maker
        app.state.burn_settings.invalidate()

    async def test_absent_policy_serves_no_burn(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """Omission must never be a change: 0.0 is what the frozen validator
        constant (MINER_EMISSION_SHARE = 1.0) already folds."""
        _install_db(app, session_maker)
        _install_chain(app)
        app.state.session_maker = session_maker
        app.state.burn_settings.invalidate()

        resp = await client.get("/api/v1/scoring/scores", headers=_ledger_headers())
        assert resp.status_code == 200
        assert resp.json()["burn_share"] == 0.0

    async def test_configured_policy_reaches_the_ledger(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_scored(session_maker, miner=_MINER, composite=0.7)
        _install_db(app, session_maker)
        _install_chain(app)
        await self._set_burn(app, session_maker, 0.35)

        resp = await client.get("/api/v1/scoring/scores", headers=_ledger_headers())
        assert resp.status_code == 200
        assert resp.json()["burn_share"] == 0.35

    async def test_stale_snapshot_replays_the_share_it_was_taken_under(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A database outage must not silently reset the burn to zero.

        The resolver reads the same database that just failed, so re-resolving
        on the stale path would hand the fleet a 0% burn for the length of the
        outage. Replaying the snapshot's own share is the only answer that does
        not move emissions because of an infrastructure problem.
        """
        await _seed_scored(session_maker, miner=_MINER, composite=0.7)
        _install_db(app, session_maker)
        _install_chain(app)
        await self._set_burn(app, session_maker, 0.5)

        ok = await client.get("/api/v1/scoring/scores", headers=_ledger_headers())
        assert ok.status_code == 200
        assert ok.json()["burn_share"] == 0.5

        app.state.ledger_snapshot.generated_at -= timedelta(
            seconds=scoring_mod._FRESH_SNAPSHOT_SECONDS + 1
        )

        async def _boom(_session: object, **_kwargs: object) -> list:
            raise OperationalError("SELECT ...", {}, Exception("db down"))

        monkeypatch.setattr(scoring_mod, "list_eligible_ledger", _boom)
        stale = await client.get("/api/v1/scoring/scores", headers=_ledger_headers())
        assert stale.status_code == 200
        assert stale.json()["stale"] is True
        assert stale.json()["burn_share"] == 0.5
