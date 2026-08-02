"""Unit tests for :mod:`ditto.api_server.endpoints.scoring`.

Exercises ``GET /scoring/scores`` against a real Postgres with the chain
permit-check mocked. The ledger read + ordering is covered at the query level in
``tests/db/queries/test_scores.py``; here we assert the endpoint's auth gate and
wire shape.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
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
from ditto.api_server.dependencies import get_chain_client, get_session
from ditto.api_server.middleware.error_envelope import ERROR_CODE_VALIDATOR_AUTH
from ditto.chain.models import NeuronInfo
from ditto.db.models import Agent, BenchmarkRollout, ValidatorHeartbeat
from ditto.db.queries.confirmation_scores import (
    ConfirmationSeedScore,
    append_confirmation_scores,
)
from ditto.db.queries.scores import upsert_score

_KEYPAIR = bittensor.Keypair.create_from_uri("//Alice")
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
) -> dict[str, str]:
    nonce = nonce or uuid4()
    requested_at = requested_at or datetime.now(UTC)
    requested = requested_at.astimezone(UTC).isoformat(timespec="microseconds")
    signed = (f"validator-ledger:v1:{_VALIDATOR_HOTKEY}:{nonce}:{requested}").encode()
    return {
        **_AUTH_HEADER,
        "X-Validator-Ledger-Nonce": str(nonce),
        "X-Validator-Ledger-Requested-At": requested_at.isoformat(),
        "X-Validator-Ledger-Signature": signing_keypair.sign(signed).hex(),
    }


def _install_db(app: FastAPI, maker: async_sessionmaker[AsyncSession]) -> None:
    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _session


def _install_chain(app: FastAPI, *, permitted: bool = True) -> None:
    async def _chain() -> MagicMock:
        c = MagicMock()
        c.get_recent_neurons = AsyncMock(
            return_value=[
                NeuronInfo(
                    hotkey=_VALIDATOR_HOTKEY,
                    coldkey="5GReceiverColdkeyPlaceholderXXXXXXXXXXXXXXXXXXX",
                    uid=1,
                    stake=1000.0,
                    validator_permit=permitted,
                )
            ]
        )
        return c

    app.dependency_overrides[get_chain_client] = _chain


async def _seed_scored(
    maker: async_sessionmaker[AsyncSession],
    *,
    miner: str,
    composite: float,
    status: AgentStatus = AgentStatus.SCORED,
) -> None:
    async with maker() as s, s.begin():
        agent = Agent(
            agent_id=uuid4(),
            miner_hotkey=miner,
            name="agent",
            sha256="ab" * 32,
            size_bytes=524288,
            status=status,
            created_at=datetime.now(UTC),
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
            n=20,
            generated_at=datetime(2026, 6, 8, 12, 0, 0, tzinfo=UTC),
            signature="ab" * 64,
            details={
                "ticket_deadline": "2026-06-08T13:00:00+00:00",
                "transcript_sha256": "cd" * 32,
            },
        )


class TestScoringLedger:
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
        self._break_db(monkeypatch)
        stale = await client.get("/api/v1/scoring/scores", headers=_ledger_headers())
        assert stale.status_code == 200
        body = stale.json()
        assert body["stale"] is True
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
