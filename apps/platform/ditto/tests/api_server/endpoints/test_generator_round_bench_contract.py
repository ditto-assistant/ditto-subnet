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
    """The active version, which round versions a validator can run, how many
    validators can, and which grading policies the generator scorer grades
    (``scorer_versions``; the explicit advertisement is covered on its own
    below)."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        active: int,
        validators: int = 3,
        scorer_versions: tuple[int, ...] = (13, 14),
    ) -> None:
        self.active = active
        self.capable: set[int] = {13, 14}
        self.validators = validators
        self.scorer_versions = scorer_versions
        self.capacity_versions: list[int] = []
        self.pin_calls = 0

        async def _active(_session: AsyncSession, **_: Any) -> int:
            return self.active

        async def _capable(
            _session: AsyncSession, *, bench_version: int, **_: Any
        ) -> int:
            return self.validators if bench_version in self.capable else 0

        def _scorer_refusal(_scorer: Any, bench_version: int) -> str | None:
            if bench_version in self.scorer_versions:
                return None
            return f"generator scorer grades {self.scorer_versions}"

        monkeypatch.setattr(rounds_mod, "version_refusal", _scorer_refusal)
        monkeypatch.setattr(
            rounds_mod, "grading_versions", lambda _scorer: self.scorer_versions
        )

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
        # The public round view shows each column's declared policy.
        public = (await client.get("/api/v1/generator/rounds/3")).json()
        shown = {c["column_id"]: c["grading_bench_version"] for c in public["columns"]}
        assert shown[summary[0]["column_id"]] == 14
        assert shown[summary[1]["column_id"]] == 13

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


class TestRoundSwitchQuorum:
    """Rounds switch to a newer bench version only once
    ``min_round_validators`` validators can run it, mirroring the agent
    rollout's ``SCORING_QUORUM`` of distinct desired-version validators."""

    async def test_below_quorum_rounds_stay_on_the_previous_version(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install(app, session_maker)
        fleet = _Fleet(monkeypatch, active=14, validators=2)
        await _apply_settings(client, validator_rounds_enabled=True)
        preview = (await client.get(_PREVIEW_URL, headers=_ADMIN)).json()
        contract = preview["bench_contract"]
        assert contract["bench_version"] == 13
        assert contract["previous_version"] == 13
        assert contract["min_round_validators"] == 3
        assert contract["scorer_grading_versions"] == [13, 14]
        assert "below min_round_validators 3" in contract["reason"]
        assert "rounds stay on v13" in contract["reason"]
        await _open_round(client, session_maker, monkeypatch)
        row = await _round(session_maker)
        assert row.status == "generating" and row.bench_version == 13
        assert (
            "below min_round_validators 3"
            in ((row.generation or {})["bench_contract"]["reason"])
        )
        fleet.validators = 3
        preview = (await client.get(_PREVIEW_URL, headers=_ADMIN)).json()
        assert preview["bench_contract"]["bench_version"] == 14
        assert preview["bench_contract"]["reason"] is None

    async def test_the_quorum_is_an_operator_setting(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install(app, session_maker)
        _Fleet(monkeypatch, active=14, validators=1)
        await _apply_settings(
            client, validator_rounds_enabled=True, min_round_validators=1
        )
        preview = (await client.get(_PREVIEW_URL, headers=_ADMIN)).json()
        assert preview["bench_contract"]["bench_version"] == 14
        assert preview["bench_contract"]["min_round_validators"] == 1

    async def test_once_switched_a_dip_below_quorum_does_not_switch_back(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The quorum gates only a switch up: a version at or below the
        previous round's needs one capable validator, as before."""
        _install(app, session_maker)
        fleet = _Fleet(monkeypatch, active=14, validators=3)
        await _open_round(client, session_maker, monkeypatch)
        assert (await _round(session_maker)).bench_version == 14
        fleet.validators = 1
        contract = (await client.get(_PREVIEW_URL, headers=_ADMIN)).json()[
            "bench_contract"
        ]
        assert contract["previous_version"] == 14
        assert contract["bench_version"] == 14 and contract["reason"] is None
        fleet.validators = 0
        contract = (await client.get(_PREVIEW_URL, headers=_ADMIN)).json()[
            "bench_contract"
        ]
        assert contract["bench_version"] is None

    async def test_rounds_never_open_at_a_version_the_scorer_does_not_grade(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A generator scorer that grades only v13 keeps rounds on v13 even
        with a full v14 validator quorum, and the preview says why."""
        _install(app, session_maker)
        _Fleet(monkeypatch, active=14, scorer_versions=(13,))
        await _apply_settings(client, validator_rounds_enabled=True)
        preview = (await client.get(_PREVIEW_URL, headers=_ADMIN)).json()
        contract = preview["bench_contract"]
        assert contract["bench_version"] == 13
        assert contract["scorer_grading_versions"] == [13]
        assert "generator scorer grades" in contract["reason"]


class TestScorerGradingVersionLease:
    async def test_a_v14_round_voids_for_a_scorer_that_grades_only_v13(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """At lease, the polling scorer's own advertisement decides: one that
        lists no grading versions is v13-only, so a v14 round voids with an
        infra reason rather than being generated on the wrong policies."""
        _install(app, session_maker)
        _Fleet(monkeypatch, active=14)
        await _open_round(client, session_maker, monkeypatch)
        assert (await _round(session_maker)).bench_version == 14
        # Real matching at lease (the fleet stubbed it for the open).
        from ditto.api_server import generator_scorer_capability as capability_mod

        monkeypatch.setattr(
            rounds_mod, "version_refusal", capability_mod.version_refusal
        )
        resp = await client.get(
            "/api/v1/generator-scorer/round-work",
            headers=_SCORER,
            params={"scorer_id": "scorer-old", "scorer_version": "challenge-scorer/5"},
        )
        assert resp.status_code == 200 and resp.json()["work"] is None
        row = await _round(session_maker)
        assert row.status == "void"
        assert (row.void_reason or "").startswith("infra: generator scorer scorer-old")
        assert "treated as v13-only" in (row.void_reason or "")


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


@pytest.mark.parametrize(
    ("error", "scorer"),
    [
        ("request grading_bench_version must be at least 13", True),
        (
            "request accepts grading policies [15 16], none of which this "
            "package supports [13 14]; rebuild against the current dittobench-datagen",
            True,
        ),
        ("generator exited 1: request accepts grading policies [15 16]", False),
    ],
)
def test_request_refusals_from_the_window_are_scorer_faults(
    error: str, scorer: bool
) -> None:
    """The scorer refusing a request outside its own grading policies is an
    infrastructure fault, never the generator's failure."""
    from ditto.api_server.generator_competition import scorer_request_error

    assert scorer_request_error(error) is scorer


@pytest.mark.parametrize(
    ("round_version", "accepted"),
    [(13, (13,)), (14, (13, 14)), (15, (14,))],
)
def test_a_round_accepts_its_version_and_the_one_before(
    round_version: int, accepted: tuple[int, ...]
) -> None:
    from ditto.api_models.generator_round_validator import accepted_grading_versions

    assert accepted_grading_versions(round_version) == accepted


def test_round_switch_quorum_mirrors_the_agent_rollout_quorum() -> None:
    """Rounds switch to a new bench version at the same quorum the agent
    rollout needs before the version takes authority: ``SCORING_QUORUM``
    desired-version scores per cohort position, one per distinct validator
    (``rollout_cohort_score_complete``). Resizing one without the other fails
    here. A revision written before the setting existed reads as it too."""
    from ditto.api_models.generator import GeneratorCompetitionSettings
    from ditto.db.queries.benchmark_rollout import SCORING_QUORUM as ROLLOUT_QUORUM
    from ditto.db.queries.scores import SCORING_QUORUM

    assert ROLLOUT_QUORUM == SCORING_QUORUM == 3
    assert GeneratorCompetitionSettings().min_round_validators == SCORING_QUORUM
    legacy = GeneratorCompetitionSettings.model_validate({"mode": "shadow"})
    assert legacy.min_round_validators == SCORING_QUORUM
