"""Generator rounds freeze their bench contract when they open.

A round runs at the active benchmark version when that version has a round
contract (``GENERATOR_ROUND_BENCH_VERSIONS``) and a validator can run it, at
the newest round contract below it otherwise, and is void when no validator
can run any. The version is frozen on the round row: an activation mid-round
never moves its tickets, jobs or capability routing.

Today the only round contract is v13 (round packs grade at
``challenge.GradingBenchVersion``), so the tests that exercise a v14 round
widen the tuple to ``(13, 14)`` the way a future pack contract would.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import ditto.api_server.endpoints.validator_generator_round as lane_mod
import ditto.api_server.generator_rounds as rounds_mod
from ditto.api_models.generator_round_validator import (
    GENERATOR_ROUND_BENCH_VERSIONS,
    LEGACY_ROUND_BENCH_VERSION,
    MAX_ROUND_COLUMN_CASES,
)
from ditto.api_models.private_dataset import PrivateDatasetRequest
from ditto.api_models.ticket_status import TicketPurpose
from ditto.api_server.generator_rounds import (
    choose_round_bench_version,
    get_round,
    round_bench_version,
    round_capable_validators,
    round_column_capacity,
)
from ditto.db.models import GeneratorRound, ValidatorTicket
from ditto.tests.api_server.endpoints.test_generator_rounds import (
    _SCORER,
    _VALIDATOR,
    _advance,
    _apply_settings,
    _generate_round,
    _install,
    _lease_job,
    _patch_lane,
    _patch_panel,
    _qualify_all,
    _register_default,
    _seed_agent,
    _signed_score,
)
from ditto.tests.db.queries.test_benchmark_rollout import _heartbeat


class _Fleet:
    """The active version and which round versions a validator can run."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, active: int) -> None:
        self.active = active
        self.capable: set[int] = {13, 14}
        self.capacity_versions: list[int] = []
        self.pin_calls = 0

        async def _active(_session: AsyncSession, **_: Any) -> int:
            return self.active

        async def _capable(
            _session: AsyncSession, *, bench_version: int, **_: Any
        ) -> int:
            return 1 if bench_version in self.capable else 0

        def _capacity(_heartbeat: Any, *, now: datetime, bench_version: int) -> Any:
            del now
            self.capacity_versions.append(bench_version)
            return MAX_ROUND_COLUMN_CASES if bench_version in self.capable else None

        monkeypatch.setattr(rounds_mod, "active_bench_version", _active)
        monkeypatch.setattr(rounds_mod, "round_capable_validators", _capable)
        # After _patch_lane, which stubs the capacity without a version.
        self._capacity = _capacity

    def install_lane(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import ditto.api_server.v13_scorer_cohort as cohort_mod

        _patch_lane(monkeypatch)
        monkeypatch.setattr(lane_mod, "_round_column_capacity", self._capacity)

        async def _pin(*_args: Any, **_kwargs: Any) -> bool:
            # The V13 scorer pin refuses this validator: a v13 round cannot
            # lease to it, a v14 round never consults the pin.
            self.pin_calls += 1
            return False

        monkeypatch.setattr(cohort_mod, "pinned_validator_allowed", _pin)


async def _open_round(
    client: httpx.AsyncClient,
    maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> list[UUID]:
    """Commit and open round 3 (epochs 12..15) with a three-agent panel."""
    await _apply_settings(client, validator_rounds_enabled=True)
    agents = [await _seed_agent(maker, rank) for rank in range(3)]
    _patch_panel(monkeypatch, agents)
    await _register_default(client)
    await _qualify_all(maker)
    await _advance(maker, 9)
    await _advance(maker, 12)
    return agents


async def _round(maker: async_sessionmaker[AsyncSession]) -> GeneratorRound:
    async with maker() as session:
        row = await get_round(session, 3)
    assert row is not None
    return row


def _download(job: dict[str, Any]) -> dict[str, Any]:
    request = PrivateDatasetRequest(
        validator_hotkey=_VALIDATOR.ss58_address,
        dataset_sha256=job["dataset_sha256"],
        deadline=datetime.fromisoformat(job["deadline"]),
        nonce=uuid4(),
        requested_at=datetime.now(UTC),
        signature="0" * 128,
    )
    request.signature = _VALIDATOR.sign(
        request.signing_message(UUID(job["agent_id"]))
    ).hex()
    return request.model_dump(mode="json")


class TestRoundBenchContract:
    async def test_a_v13_round_stays_v13_after_activation_flips(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(rounds_mod, "GENERATOR_ROUND_BENCH_VERSIONS", (13, 14))
        _install(app, session_maker)
        fleet = _Fleet(monkeypatch, active=13)
        fleet.install_lane(monkeypatch)
        await _open_round(client, session_maker, monkeypatch)
        row = await _round(session_maker)
        assert row.status == "generating" and row.bench_version == 13
        assert "bench_contract" not in (row.generation or {})
        case_ids = await _generate_round(client)

        # v14 activates mid-round, and the pin now admits this validator.
        fleet.active = 14
        import ditto.api_server.v13_scorer_cohort as cohort_mod

        async def _admit(*_args: Any, **_kwargs: Any) -> bool:
            return True

        monkeypatch.setattr(cohort_mod, "pinned_validator_allowed", _admit)
        job = await _lease_job(client)
        assert job is not None
        assert job["bench_version"] == 13
        assert set(fleet.capacity_versions) == {13}
        async with session_maker() as session:
            ticket = await session.get(
                ValidatorTicket, (UUID(job["agent_id"]), 13, _VALIDATOR.ss58_address)
            )
            assert ticket is not None
            assert ticket.purpose == TicketPurpose.GENERATOR_ROUND
            assert (
                await session.get(
                    ValidatorTicket,
                    (UUID(job["agent_id"]), 14, _VALIDATOR.ss58_address),
                )
                is None
            )
        scored = await client.post(
            f"/api/v1/validator/agent/{job['agent_id']}/generator-round-score",
            json=_signed_score(job, case_ids, 0.5),
        )
        assert scored.status_code == 200, scored.text
        assert (await _round(session_maker)).bench_version == 13

    async def test_a_round_opened_after_activation_runs_at_the_new_version(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(rounds_mod, "GENERATOR_ROUND_BENCH_VERSIONS", (13, 14))
        _install(app, session_maker)
        fleet = _Fleet(monkeypatch, active=14)
        fleet.install_lane(monkeypatch)
        await _open_round(client, session_maker, monkeypatch)
        assert (await _round(session_maker)).bench_version == 14
        case_ids = await _generate_round(client)

        job = await _lease_job(client)
        assert job is not None, "a v14 round never consults the V13 scorer pin"
        assert job["bench_version"] == 14
        assert job["inference"] is not None
        assert fleet.pin_calls == 0
        assert set(fleet.capacity_versions) == {14}
        async with session_maker() as session:
            ticket = await session.get(
                ValidatorTicket, (UUID(job["agent_id"]), 14, _VALIDATOR.ss58_address)
            )
            assert ticket is not None
            assert ticket.purpose == TicketPurpose.GENERATOR_ROUND
            row = await get_round(session, 3)
            assert row is not None and row.dataset_bytes is not None
            dataset = row.dataset_bytes
        body = await client.post(
            f"/api/v1/validator/agent/{job['agent_id']}/private-dataset",
            json=_download(job),
        )
        assert body.status_code == 200, body.text
        assert body.content == dataset
        scored = await client.post(
            f"/api/v1/validator/agent/{job['agent_id']}/generator-round-score",
            json=_signed_score(job, case_ids, 0.5),
        )
        assert scored.status_code == 200, scored.text

    async def test_a_v13_round_keeps_the_v13_scorer_pin(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install(app, session_maker)
        fleet = _Fleet(monkeypatch, active=13)
        fleet.install_lane(monkeypatch)
        await _open_round(client, session_maker, monkeypatch)
        await _generate_round(client)
        assert await _lease_job(client) is None
        assert fleet.pin_calls > 0

    async def test_leases_route_only_to_validators_capable_of_the_round(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(rounds_mod, "GENERATOR_ROUND_BENCH_VERSIONS", (13, 14))
        _install(app, session_maker)
        fleet = _Fleet(monkeypatch, active=14)
        fleet.install_lane(monkeypatch)
        await _open_round(client, session_maker, monkeypatch)
        assert (await _round(session_maker)).bench_version == 14
        await _generate_round(client)

        # This validator's scorer is verified only for v13 now.
        fleet.capable = {13}
        assert await _lease_job(client) is None
        assert fleet.capacity_versions and set(fleet.capacity_versions) == {14}
        fleet.capable = {13, 14}
        job = await _lease_job(client)
        assert job is not None and job["bench_version"] == 14

    async def test_active_v14_without_a_round_contract_falls_back_to_v13(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Today's tuple: v14 has no round contract, so rounds stay on v13 and
        say why."""
        assert GENERATOR_ROUND_BENCH_VERSIONS == (13,)
        _install(app, session_maker)
        _Fleet(monkeypatch, active=14)
        await _open_round(client, session_maker, monkeypatch)
        row = await _round(session_maker)
        assert row.status == "generating" and row.bench_version == 13
        contract = (row.generation or {})["bench_contract"]
        assert contract["active_version"] == 14 and contract["bench_version"] == 13
        assert "v14 has no generator-round contract" in contract["reason"]

    async def test_falls_back_when_no_validator_runs_the_active_contract(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(rounds_mod, "GENERATOR_ROUND_BENCH_VERSIONS", (13, 14))
        _install(app, session_maker)
        fleet = _Fleet(monkeypatch, active=14)
        fleet.capable = {13}
        await _open_round(client, session_maker, monkeypatch)
        row = await _round(session_maker)
        assert row.status == "generating" and row.bench_version == 13
        reason = (row.generation or {})["bench_contract"]["reason"]
        assert "no validator can run a" in reason and "at bench v14" in reason

    async def test_a_round_is_void_when_no_validator_can_run_any_contract(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install(app, session_maker)
        fleet = _Fleet(monkeypatch, active=13)
        fleet.capable = set()
        await _open_round(client, session_maker, monkeypatch)
        row = await _round(session_maker)
        assert row.status == "void" and row.bench_version is None
        assert row.void_reason is not None
        assert row.void_reason.startswith(
            "no generator-round contract can run at active bench v13"
        )
        assert "no validator can run a" in row.void_reason
        # A void round never reaches the scorer.
        work = (
            await client.get("/api/v1/generator-scorer/round-work", headers=_SCORER)
        ).json()
        assert work.get("work") is None


async def test_choose_keeps_v13_when_the_active_version_predates_rounds(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _active(_session: AsyncSession, **_: Any) -> int:
        return 9

    async def _capable(*_args: Any, **_kwargs: Any) -> int:
        return 1

    monkeypatch.setattr(rounds_mod, "active_bench_version", _active)
    monkeypatch.setattr(rounds_mod, "round_capable_validators", _capable)
    contract = await choose_round_bench_version(
        session, cases_per_column=40, now=datetime.now(UTC)
    )
    assert contract.bench_version == 13
    assert contract.reason is not None and "predates" in contract.reason


def test_a_round_without_a_frozen_version_ran_at_v13() -> None:
    legacy = GeneratorRound(bench_version=None)
    assert round_bench_version(legacy) == LEGACY_ROUND_BENCH_VERSION == 13
    assert round_bench_version(GeneratorRound(bench_version=14)) == 14


def _round_heartbeat(hotkey: str, now: datetime, versions: list[int]) -> Any:
    heartbeat = _heartbeat(hotkey, now, versions=versions, protocol_version=18)
    assert heartbeat.capabilities is not None
    heartbeat.capabilities["sandbox_memory_limit"] = True
    scorer = heartbeat.capabilities["scorer_benchmarks"]
    scorer["generator_rounds"] = True
    scorer["generator_round_max_cases"] = 80
    return heartbeat


def test_round_capacity_is_gated_on_the_round_version() -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    v13_only = _round_heartbeat("5" + "C" * 47, now, [7, 13])
    assert round_column_capacity(v13_only, now=now, bench_version=13) == 80
    assert round_column_capacity(v13_only, now=now, bench_version=14) is None


async def test_capable_validators_count_by_version_and_v13_pin(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ditto.api_server.v13_scorer_cohort as cohort_mod

    now = datetime.now(UTC).replace(microsecond=0)
    session.add(_round_heartbeat("5" + "D" * 47, now, [7, 13]))
    await session.flush()
    assert (
        await round_capable_validators(
            session, bench_version=13, cases_per_column=40, now=now
        )
        == 1
    )
    assert (
        await round_capable_validators(
            session, bench_version=14, cases_per_column=40, now=now
        )
        == 0
    )
    # A column larger than the scorer runs does not count.
    assert (
        await round_capable_validators(
            session, bench_version=13, cases_per_column=81, now=now
        )
        == 0
    )

    async def _refuse(*_args: Any, **_kwargs: Any) -> bool:
        return False

    monkeypatch.setattr(cohort_mod, "pinned_validator_allowed", _refuse)
    assert (
        await round_capable_validators(
            session, bench_version=13, cases_per_column=40, now=now
        )
        == 0
    )
