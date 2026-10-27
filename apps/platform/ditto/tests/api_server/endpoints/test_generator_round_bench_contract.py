"""Generator rounds freeze their bench contract when they open.

A round runs at the active benchmark version when that version has a round
contract (``GENERATOR_ROUND_BENCH_VERSIONS``) and a validator can run it, at
the newest round contract below it otherwise, and is void when no validator
can run any. The version is frozen on the round row: an activation mid-round
never moves its tickets, jobs or capability routing.

The round contracts are the pack grading policies v13 and v14
(``challenge.SupportedGradingBenchVersions``): a v14 round accepts v13 and v14
packs, each column graded at the policy its pack declares.
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
    round_contract_for,
)
from ditto.db.models import GeneratorRound, ValidatorTicket
from ditto.tests.api_server.endpoints.test_generator_rounds import (
    _ADMIN,
    _ROUNDS_URL,
    _SCORER,
    _VALIDATOR,
    _advance,
    _apply_settings,
    _generate_round,
    _generation,
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

_PREVIEW_URL = f"{_ROUNDS_URL}/preview"


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

    async def test_active_version_without_a_round_contract_falls_back(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A version with no pack grading policy (v15 today) keeps rounds on
        the newest round contract below it, and the round says why."""
        assert GENERATOR_ROUND_BENCH_VERSIONS == (13, 14)
        _install(app, session_maker)
        _Fleet(monkeypatch, active=15)
        await _open_round(client, session_maker, monkeypatch)
        row = await _round(session_maker)
        assert row.status == "generating" and row.bench_version == 14
        contract = (row.generation or {})["bench_contract"]
        assert contract["active_version"] == 15 and contract["bench_version"] == 14
        assert "v15 has no generator-round contract" in contract["reason"]

    async def test_v14_round_generation_names_and_records_the_pack_policy(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The generate work names the round's version; each column records
        the policy its pack declared, and a v13 column (a generator that
        predates v14, here after the baseline retry) keeps its place."""
        _install(app, session_maker)
        _Fleet(monkeypatch, active=14)
        await _open_round(client, session_maker, monkeypatch)
        work = (
            await client.get("/api/v1/generator-scorer/round-work", headers=_SCORER)
        ).json()["work"]
        assert work["kind"] == "generate" and work["bench_version"] == 14
        generated = _generation(work)
        policies = [14, 13]
        for column, policy in zip(generated["columns"], policies, strict=False):
            column["seed"]["grading_bench_version"] = policy
        assert len(generated["columns"]) >= 2, "need a v14 and a v13 column"
        generated["columns"][1]["seed"]["legacy_retry"] = "retried on v13"
        resp = await client.post(
            "/api/v1/generator-scorer/round-results", headers=_SCORER, json=generated
        )
        assert resp.status_code == 200 and resp.json()["status"] == "running", resp.text
        row = await _round(session_maker)
        summary = (row.generation or {})["columns"]
        assert summary[0]["grading_bench_version"] == 14
        assert summary[1]["grading_bench_version"] == 13
        assert summary[1]["legacy_retry"] == "retried on v13"

    async def test_v13_round_refuses_a_v14_pack(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install(app, session_maker)
        _Fleet(monkeypatch, active=13)
        await _open_round(client, session_maker, monkeypatch)
        work = (
            await client.get("/api/v1/generator-scorer/round-work", headers=_SCORER)
        ).json()["work"]
        assert work["bench_version"] == 13
        generated = _generation(work)
        generated["columns"][0]["seed"]["grading_bench_version"] = 14
        resp = await client.post(
            "/api/v1/generator-scorer/round-results", headers=_SCORER, json=generated
        )
        assert resp.status_code != 200
        assert "does not accept" in resp.text
        # The original v13 shape records no policy.
        generated["columns"][0]["seed"].pop("grading_bench_version")
        resp = await client.post(
            "/api/v1/generator-scorer/round-results", headers=_SCORER, json=generated
        )
        assert resp.status_code == 200, resp.text
        row = await _round(session_maker)
        assert all(
            "grading_bench_version" not in column
            for column in (row.generation or {})["columns"]
        )

    async def test_falls_back_when_no_validator_runs_the_active_contract(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
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


class TestRoundPreviewBenchContract:
    async def test_preview_names_the_contract_a_round_would_freeze(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Backroom's round preview shows the version a round opened now
        would run at, why, and the capable validators per contract."""
        _install(app, session_maker)
        fleet = _Fleet(monkeypatch, active=14)
        await _apply_settings(client, validator_rounds_enabled=True)
        fleet.capable = {13}
        preview = (await client.get(_PREVIEW_URL, headers=_ADMIN)).json()
        contract = preview["bench_contract"]
        assert contract["active_version"] == 14 and contract["bench_version"] == 13
        assert contract["round_contracts"] == [13, 14]
        assert contract["capable_validators"]["13"] > 0
        assert contract["capable_validators"]["14"] == 0
        assert "at bench v14" in contract["reason"]
        fleet.capable = {13, 14}
        preview = (await client.get(_PREVIEW_URL, headers=_ADMIN)).json()
        assert preview["bench_contract"]["bench_version"] == 14
        assert preview["bench_contract"]["reason"] is None
        fleet.capable = set()
        preview = (await client.get(_PREVIEW_URL, headers=_ADMIN)).json()
        assert preview["bench_contract"]["bench_version"] is None
        assert any("no generator-round contract" in b for b in preview["blockers"])


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


def test_round_capacity_requires_the_scorer_round_mode_for_the_version() -> None:
    """A scorer verified for v14 runs a v14 round only when its round mode
    advertises v14; one advertising no round versions runs only v13 rounds."""
    now = datetime.now(UTC).replace(microsecond=0)
    legacy_mode = _round_heartbeat("5" + "E" * 47, now, [7, 13, 14])
    assert round_column_capacity(legacy_mode, now=now, bench_version=14) is None
    assert round_column_capacity(legacy_mode, now=now, bench_version=13) == 80
    v14_mode = _round_heartbeat("5" + "E" * 47, now, [7, 13, 14])
    assert v14_mode.capabilities is not None
    v14_mode.capabilities["scorer_benchmarks"]["generator_round_bench_versions"] = [
        13,
        14,
    ]
    assert round_column_capacity(v14_mode, now=now, bench_version=13) == 80
    assert round_column_capacity(v14_mode, now=now, bench_version=14) == 80
    v13_mode = _round_heartbeat("5" + "E" * 47, now, [7, 13, 14])
    assert v13_mode.capabilities is not None
    v13_mode.capabilities["scorer_benchmarks"]["generator_round_bench_versions"] = [13]
    assert round_column_capacity(v13_mode, now=now, bench_version=14) is None


@pytest.mark.parametrize(
    ("active", "contract"), [(9, 13), (13, 13), (14, 14), (15, 14)]
)
def test_round_contract_for_the_active_version(active: int, contract: int) -> None:
    assert round_contract_for(active) == contract


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
