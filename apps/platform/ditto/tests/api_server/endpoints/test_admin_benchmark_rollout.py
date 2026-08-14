"""HTTP contract tests for the guarded benchmark rollout control plane."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from ditto.api_models.agent_status import AgentStatus
from ditto.api_server.datapipeline import DataPipelineError
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints import admin_benchmark_rollout
from ditto.api_server.endpoints.admin_benchmark_rollout import (
    MINIMUM_ROLLOUT_START_VALIDATORS,
    _inference_proxy_start_blocker,
)
from ditto.api_server.inference_routing import benchmark_model
from ditto.api_server.middleware.error_envelope import (
    ERROR_CODE_HTTP_EXCEPTION,
    ERROR_CODE_UNHANDLED,
)
from ditto.db.models import (
    Agent,
    BenchmarkRollout,
    BenchmarkRolloutAudit,
    BenchmarkRolloutMember,
    InferenceProviderRoute,
    InferenceRoutingPolicy,
    Score,
    ValidatorHeartbeat,
)
from ditto.db.queries.benchmark_rollout import (
    MIN_SCOREABLE_BENCH_VERSION,
    DatasetPin,
    RolloutSnapshotMember,
    create_rollout_snapshot,
)
from ditto.tests.legacy_era import (
    retired_era_writes_allowed,
)

pytestmark = pytest.mark.asyncio

_TOKEN = "test-admin-token-at-least-32-characters"
_HEADERS = {"Authorization": f"Bearer {_TOKEN}"}
# The rollout these tests start. V8 is shipped as a target while v7 remains the
# active era; only the explicit guarded start below may open the transition.
_TARGET = 8
# The message the deployed generate-service actually returned when the rollout
# was started from the operator console against a datagen release that only
# ships v2 and v3. Quoted verbatim, so it keeps naming the versions it named.
_LAGGING = "bench_version query param required (supported: 2, 3)"


@pytest.fixture
def instrumented_maker(
    engine: AsyncEngine,
) -> tuple[async_sessionmaker[AsyncSession], list[str]]:
    """A session maker that records every statement the handler actually runs.

    The listener is attached to the root ``engine`` fixture *after* it has
    reset the worker database, so the reset's own DDL is not recorded.
    """
    statements: list[str] = []

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _record(
        _connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        statements.append(statement)

    return async_sessionmaker(engine, expire_on_commit=False), statements


_V7_MODEL = benchmark_model(_TARGET)
_MANIFEST = "c" * 64


async def test_post_v7_proxy_preflight_does_not_require_calibration_manifest(
    app: FastAPI,
) -> None:
    config = replace(
        app.state.config.inference_proxy,
        enabled=True,
        openrouter_api_key="test-openrouter-key",
        reviewed_calibration_manifest_sha256=None,
    )

    assert _inference_proxy_start_blocker(8, config) is None
    assert _inference_proxy_start_blocker(9, config) is None
    assert "reviewed calibration manifest" in str(
        _inference_proxy_start_blocker(7, config)
    )


def _install(app: FastAPI, maker: async_sessionmaker[AsyncSession]) -> None:
    """Point the app at the test database with a v7-ready inference proxy.

    The proxy settings are part of the fixture now because the target is v7:
    starting a v7 rollout is gated on an enabled proxy holding a provider key
    and a reviewed calibration manifest, and the default test config has none
    of the three. Leaving them unset would make every start below fail on that
    gate instead of on the generate-service behaviour under test.
    """
    app.state.config = replace(
        app.state.config,
        admin_api_token=_TOKEN,
        inference_proxy=replace(
            app.state.config.inference_proxy,
            enabled=True,
            openrouter_api_key="test-openrouter-key",
            routing_mode="adaptive",
            reviewed_calibration_manifest_sha256=_MANIFEST,
        ),
    )

    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session


class _StubGenerator:
    """The separately deployed generate-service, present or lagging.

    ``run_size`` must be set: a ``None`` run size is read as "generation
    disabled" and blocks qualification before any call is attempted, which
    would make these tests pass for the wrong reason.
    """

    run_size = "full"

    def __init__(self, error: DataPipelineError | None = None) -> None:
        self._error = error
        self.calls: list[tuple[int, int]] = []

    async def generate(self, seed: int, bench_version: int = 2) -> str:
        self.calls.append((seed, bench_version))
        if self._error is not None:
            raise self._error
        return f"{seed:064x}"

    async def aclose(self) -> None:
        return None


def _capabilities(now: datetime) -> tuple[dict[str, Any], dict[str, Any]]:
    revision = "a" * 40
    # A v7-capable validator has to advertise more than the version number:
    # ticket inference, signed score quorum and a calibration manifest naming
    # the routes it can actually reach. Without them ``verified_scorer_for_version``
    # counts this heartbeat as incapable and the start fails on the capacity
    # gate rather than on anything these tests are about.
    capabilities = {
        "screened_images": True,
        "require_screened_image": False,
        "source_build_fallback": True,
        "full_stack_managed": False,
        "stack_updater": False,
        "sandbox_egress_restricted": True,
        "ticket_inference": True,
        "signed_score_quorum": True,
        # Production validators may intentionally use the signed privileged
        # DinD boundary when the scorer policy does not require rootless.
        "executor_isolation": "privileged_dind",
        "scorer_benchmarks": {
            "status": "fresh_verified",
            "supported_bench_versions": [2, 3, 7, _TARGET],
            "observed_at": int(now.timestamp()),
            "software_version": "1.3.0",
            "source_revision": revision,
            "v7_calibration": {
                "manifest_sha256": _MANIFEST,
                "supported_routes": [
                    {
                        "provider": "Groq",
                        "profile_revision": "openrouter-route-test-v1",
                        "model": _V7_MODEL,
                    }
                ],
            },
        },
    }
    stack = {
        "mode": "source",
        "compose_schema": 1,
        "release_descriptor_digest": None,
        "components": {
            name: {
                "source_revision": revision if name == "dittobench_api" else "b" * 40,
                "version": "1.3.0" if name == "dittobench_api" else "1.2.0",
                "provenance": "committed_pin",
            }
            for name in (
                "ditto_subnet",
                "dittobench_api",
                "sandbox_docker",
                "model_relay",
                "pylon",
                "ollama",
            )
        },
    }
    return capabilities, stack


def _add_cohort_agent(
    session: AsyncSession,
    *,
    position: int,
    composite: float,
    now: datetime,
    bench_version: int = 2,
) -> RolloutSnapshotMember:
    """Add one SCORED agent with a full source-era quorum, top-five eligible.

    Historical tests retain the v2 default and seed inside
    ``retired_era_writes_allowed``. The v8 transition passes v7 explicitly.
    """
    agent_id = uuid4()
    miner = f"miner-{position}"
    digest = f"{position:x}" * 64
    session.add(
        Agent(
            agent_id=agent_id,
            miner_hotkey=miner,
            name=f"agent-{position}",
            sha256=digest,
            status=AgentStatus.SCORED,
            screening_policy_version=9,
            screened_image_sha256=digest,
            screened_image_size_bytes=1024,
            screened_image_id=f"sha256:{digest}",
            screened_image_ref=f"ditto-screen/{agent_id}:latest",
            screened_image_upload_id=uuid4(),
            screened_image_verified_at=now,
            created_at=now + timedelta(seconds=position),
        )
    )
    for validator in range(3):
        session.add(
            Score(
                agent_id=agent_id,
                bench_version=bench_version,
                validator_hotkey=f"source-{validator}",
                run_id=f"v{bench_version}-{position}-{validator}",
                signature="aa",
                seed=position,
                composite=composite,
                tool_mean=0.5,
                memory_mean=0.5,
                median_ms=1,
                n=114,
                details={"bench_version": bench_version},
                generated_at=now,
            )
        )
    return RolloutSnapshotMember(
        agent_id=agent_id, miner_hotkey=miner, composite=composite
    )


def _add_ready_route(session: AsyncSession, now: datetime) -> None:
    """One healthy, reviewed-calibration route for the v7 consensus model.

    The second half of the v7 start gate: past the config check the handler
    still requires a live route whose calibration manifest is the reviewed one.
    Same shape the rollout-query tests use, pinned to the manifest ``_install``
    configures so the two halves agree.
    """
    session.add(
        InferenceRoutingPolicy(
            model=_V7_MODEL,
            enabled=True,
            speed_weight=0.65,
            cost_weight=0.25,
            exploration_weight=0.10,
            exploration_ticket_budget=3,
            min_tool_accuracy=0.55,
            min_composite=0.15,
            min_calibration_samples=20,
            max_error_rate=0.25,
            max_timeout_rate=0.15,
            cooldown_seconds=30,
            ewma_alpha=0.20,
            updated_at=now,
        )
    )
    session.add(
        InferenceProviderRoute(
            model=_V7_MODEL,
            provider="Groq",
            profile_revision="openrouter-route-test-v1",
            status="healthy",
            calibration_status="eligible",
            calibration_tool_accuracy=0.65,
            calibration_composite=0.20,
            calibration_sample_count=60,
            calibration_manifest_sha256=_MANIFEST,
            ewma_error_rate=0,
            ewma_timeout_rate=0,
            sample_count=60,
            selected_ticket_count=0,
            exploration_ticket_count=0,
            discovered_at=now,
            last_observed_at=now,
            updated_at=now,
        )
    )


async def _seed_start_ready(
    maker: async_sessionmaker[AsyncSession], now: datetime
) -> list[RolloutSnapshotMember]:
    """Seed the five-miner cohort and just enough capable validators to start.

    Also records the v7 activation that is in force before the v8 transition.
    This is the production-shaped forward transition and avoids reaching into
    a retired era for a test that is not about retired history.
    """
    capabilities, stack = _capabilities(now)
    async with maker() as session, session.begin():
        session.add(
            BenchmarkRollout(
                rollout_id=uuid4(),
                from_version=6,
                desired_version=7,
                status="activated",
                cohort_size=5,
                created_at=now - timedelta(days=30),
                activated_at=now - timedelta(days=30),
            )
        )
        _add_ready_route(session, now)
        members = [
            _add_cohort_agent(
                session,
                position=position,
                composite=0.5 + position / 100,
                now=now,
                bench_version=7,
            )
            for position in range(1, 6)
        ]
        for index in range(MINIMUM_ROLLOUT_START_VALIDATORS):
            session.add(
                ValidatorHeartbeat(
                    validator_hotkey=f"validator-{index}",
                    software_version="1.0.0",
                    # Protocol 12 is the wire-format floor for advertising v7 at
                    # all; a protocol-8 heartbeat is rejected before its
                    # capabilities are even read.
                    protocol_version=12,
                    code_digest="d" * 64,
                    state="polling",
                    first_seen_at=now,
                    reported_at=now,
                    seen_at=now,
                    signature="ab" * 64,
                    capabilities=capabilities,
                    stack=stack,
                )
            )
    return members


async def _rollout_count(maker: async_sessionmaker[AsyncSession]) -> int:
    """How many rollouts exist for the transition under test.

    Scoped to ``_TARGET`` rather than counting the whole table: the fixture also
    seeds the activation that puts the ledger on v7, and "did the start leave a
    rollout behind" is a question about the v8 transition, not
    about the history it starts from.
    """
    async with maker() as session:
        return int(
            await session.scalar(
                select(func.count(BenchmarkRollout.rollout_id)).where(
                    BenchmarkRollout.desired_version == _TARGET
                )
            )
            or 0
        )


async def _seed_expandable_rollout(
    maker: async_sessionmaker[AsyncSession], now: datetime
) -> list[RolloutSnapshotMember]:
    members = await _seed_start_ready(maker, now)
    async with maker() as session, session.begin():
        members.extend(
            _add_cohort_agent(
                session,
                position=position,
                composite=0.5 - position / 100,
                now=now,
                bench_version=7,
            )
            for position in range(6, 16)
        )
        await create_rollout_snapshot(
            session,
            members=members[:10],
            datasets={
                member.agent_id: DatasetPin(
                    seed=index, sha256=f"{index:064x}", run_size="full"
                )
                for index, member in enumerate(members[:10], start=1)
            },
            now=now,
            from_version=7,
            desired_version=_TARGET,
            rescore_cohort_target=10,
            priority_cohort_target=5,
        )
    return members


def _start_payload() -> dict[str, Any]:
    return {
        "reason": f"start the v{_TARGET} rollout",
        "actor": "backroom:test",
        "confirmation": f"START BENCHMARK V{_TARGET}",
        "expected_active_version": 7,
    }


def _expand_payload(
    *, expected_target: int = 10, new_target: int = 15
) -> dict[str, Any]:
    return {
        "reason": "expand the current v8 rollout to the intended top fifteen",
        "actor": "backroom:test",
        "confirmation": f"EXPAND BENCHMARK V{_TARGET} TO {new_target}",
        "expected_active_version": 7,
        "expected_current_target": expected_target,
        "new_target": new_target,
    }


async def test_control_discovery_is_authenticated_read_only_and_dynamic(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)

    denied = await client.get("/api/v1/admin/benchmark-rollout")
    assert denied.status_code == 401

    response = await client.get("/api/v1/admin/benchmark-rollout", headers=_HEADERS)
    assert response.status_code == 200, response.text
    body = response.json()
    # Nothing has activated in this database, so the ledger's honest answer is
    # the floor rather than the version the subnet started on.
    assert body["active_version"] == MIN_SCOREABLE_BENCH_VERSION
    assert body["status"] == "inactive"
    # Nothing is offered. A target must be both above the active version and at
    # or above the floor. V8 through v10 are discoverable but remain inert until
    # an authenticated operator starts one.
    assert body["available_target_versions"] == [8, 9, 10]
    # Still derived from the shipped registry, which is what "dynamic" means
    # here: the floor filters what may be STARTED, not what exists.
    assert [contract["version"] for contract in body["contracts"]] == [
        2,
        3,
        4,
        5,
        6,
        7,
        8,
        9,
        10,
    ]
    assert all(
        contract["capable_validator_count"] == 0 for contract in body["contracts"]
    )


async def test_control_offers_newer_contracts_without_moving_active_v8_authority(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    now = datetime.now(UTC).replace(microsecond=0)
    async with session_maker() as session, session.begin():
        session.add(
            BenchmarkRollout(
                rollout_id=uuid4(),
                from_version=7,
                desired_version=8,
                status="activated",
                cohort_size=5,
                created_at=now - timedelta(days=1),
                activated_at=now - timedelta(days=1),
            )
        )

    response = await client.get("/api/v1/admin/benchmark-rollout", headers=_HEADERS)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["active_version"] == 8
    assert body["desired_version"] == 8
    assert body["status"] == "activated"
    assert body["available_target_versions"] == [9, 10]
    assert body["contracts"][-1] == {
        "version": 10,
        "minimum_screening_policy_version": 9,
        "requires_screened_image": True,
        "capable_validator_count": 0,
        "start_ready": False,
        "start_blockers": [
            "benchmark v10 rollout requires at least 1 fresh, identity-matched "
            "v10 scorer validators"
        ],
    }

    async with session_maker() as session:
        assert (
            await session.scalar(select(func.count(BenchmarkRollout.rollout_id))) == 1
        )


async def test_control_reads_the_cohort_once_and_never_writes(
    app: FastAPI,
    client: httpx.AsyncClient,
    instrumented_maker: tuple[async_sessionmaker[AsyncSession], list[str]],
) -> None:
    """One page load is one cohort read, whatever the console is showing.

    The operator console loads this on every view. It used to derive the whole
    cohort/quorum picture once per *shipped contract* just to read one integer
    out of each, so shipping a benchmark made the operator's status page slower
    -- and with the per-case ``details`` breakdown hydrated for every scored
    agent on each pass, slow enough to blow through the console's own timeout.
    """
    maker, statements = instrumented_maker
    _install(app, maker)
    now = datetime.now(UTC).replace(microsecond=0)
    members = await _seed_start_ready(maker, now)
    async with maker() as session, session.begin():
        await create_rollout_snapshot(
            session,
            members=members,
            datasets={
                member.agent_id: DatasetPin(
                    seed=index, sha256="c" * 64, run_size="full"
                )
                for index, member in enumerate(members, start=1)
            },
            now=now,
            from_version=2,
            desired_version=_TARGET,
        )
    generator = _StubGenerator()
    app.state.dataset_generator = generator
    statements.clear()

    response = await client.get("/api/v1/admin/benchmark-rollout", headers=_HEADERS)

    assert response.status_code == 200, response.text
    # Flat in the number of shipped contracts. Six contracts once cost 106
    # statements here; the count must not track ``benchmark_contracts()``.
    assert len(statements) <= 30, "\n".join(statements)
    # And the ranking read must not drag the per-case breakdown along with it:
    # that column is kilobytes per score row, for every scored agent.
    assert all("details" not in statement for statement in statements), statements
    # A read is a read: no rollout is opened, advanced, or activated, and the
    # generate-service is never dialled from the status path.
    assert not [
        statement
        for statement in statements
        if statement.lstrip()[:6].upper() in ("INSERT", "UPDATE", "DELETE")
    ]
    assert generator.calls == []
    assert await _rollout_count(maker) == 1


async def test_control_degrades_the_slow_section_instead_of_hanging(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lagging qualification lookup costs that section, not the whole page.

    The budget is shared: the handler spends it on the core read first and only
    then on the qualification loop. So this test is only meaningful while the
    *real* core read fits inside the budget -- otherwise the core read times out
    and the handler 503s, which is the sibling test's scenario, not this one.

    On in-memory SQLite the core read was ~1ms and 0.05s was ample. On a real
    Postgres under ``-n auto`` it measures 39-132ms, i.e. straddling a 0.05s
    budget, which made this test fail roughly one run in three. 2.0s clears the
    measured worst case by ~15x while staying far under both the 12.0s
    production value and the 60s stub hang. Nothing asserted below changed; this
    is the test's own speed knob, not a property of the endpoint.
    """
    _install(app, session_maker)
    # There has to be a candidate target for the qualification loop to be slow
    # ABOUT. Candidates are the shipped contracts above the active version and
    # at or above the floor, so on an empty database -- where the ledger
    # answers the floor -- that list is empty, the loop never runs and the stub
    # below is never called: the test would pass while proving nothing.
    #
    # Putting the ledger on v7 leaves exactly one candidate (v8) to hang on.
    async with session_maker() as session, session.begin():
        now = datetime.now(UTC).replace(microsecond=0) - timedelta(days=30)
        session.add(
            BenchmarkRollout(
                rollout_id=uuid4(),
                from_version=6,
                desired_version=7,
                status="activated",
                cohort_size=5,
                created_at=now,
                activated_at=now,
            )
        )

    async def _slow(*_args: object, **_kwargs: object) -> dict[str, object]:
        await asyncio.sleep(60)
        raise AssertionError("the budget should have expired first")

    monkeypatch.setattr(admin_benchmark_rollout, "ROLLOUT_STATUS_BUDGET_SECONDS", 2.0)
    monkeypatch.setattr(admin_benchmark_rollout, "authority_selection_state", _slow)

    response = await client.get("/api/v1/admin/benchmark-rollout", headers=_HEADERS)

    assert response.status_code == 200, response.text
    body = response.json()
    # The durable rollout status -- the part the operator came for -- survives.
    # ``activated`` rather than ``inactive`` because the seeded authority is a
    # real rollout row: nothing is open, and the last transition finished.
    assert body["active_version"] == _TARGET - 1
    assert body["status"] == "activated"
    assert [contract["version"] for contract in body["contracts"]] == [
        2,
        3,
        4,
        5,
        6,
        7,
        8,
        9,
        10,
    ]
    # Fail closed on what could not be proven: no candidate is offered for
    # activation, and the omission is named rather than mistaken for "none".
    assert body["active_contract_candidates"] == []
    assert body["degraded_sections"] == [
        "active_contract_candidates",
        "start_readiness",
    ]


async def test_control_names_the_database_when_the_core_read_overruns(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the core state there is nothing useful to return -- so say why.

    The operator chased a missing endpoint and a bad admin token for an hour
    because a slow read reached them as a bare client-side abort. A 503 that
    names the database is the whole point of bounding this server-side.
    """
    _install(app, session_maker)

    async def _slow(*_args: object, **_kwargs: object) -> dict[str, object]:
        await asyncio.sleep(60)
        raise AssertionError("the budget should have expired first")

    monkeypatch.setattr(admin_benchmark_rollout, "ROLLOUT_STATUS_BUDGET_SECONDS", 0.05)
    monkeypatch.setattr(admin_benchmark_rollout, "rollout_state", _slow)

    response = await client.get("/api/v1/admin/benchmark-rollout", headers=_HEADERS)

    assert response.status_code == 503, response.text
    message = response.json()["message"]
    assert "read budget" in message
    assert "database" in message
    assert "token" in message


async def test_start_requires_full_guard_payload_and_exact_confirmation(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)

    missing = await client.post("/api/v1/admin/benchmark-rollout/4", headers=_HEADERS)
    assert missing.status_code == 422

    wrong = await client.post(
        "/api/v1/admin/benchmark-rollout/4",
        headers=_HEADERS,
        json={
            "reason": "prepare the v4 rollout",
            "actor": "backroom:test",
            "confirmation": "START BENCHMARK V3",
            "expected_active_version": 2,
        },
    )
    assert wrong.status_code == 409
    assert "START BENCHMARK V4" in wrong.json()["message"]

    unsupported = await client.post(
        "/api/v1/admin/benchmark-rollout/11",
        headers=_HEADERS,
        json={
            "reason": "attempt an unshipped contract",
            "actor": "backroom:test",
            "confirmation": "START BENCHMARK V11",
            "expected_active_version": 2,
        },
    )
    assert unsupported.status_code == 409
    assert "no shipped contract" in unsupported.json()["message"]


async def test_expand_open_rollout_appends_ordered_suffix_atomically(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    now = datetime.now(UTC).replace(microsecond=0)
    members = await _seed_expandable_rollout(session_maker, now)
    generator = _StubGenerator()
    app.state.dataset_generator = generator

    response = await client.post(
        f"/api/v1/admin/benchmark-rollout/{_TARGET}/expand",
        headers=_HEADERS,
        json=_expand_payload(),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["rescore_cohort_target"] == 15
    assert body["cohort_size"] == 15
    assert body["expansion"] == {
        "previous_target": 10,
        "new_target": 15,
        "appended_members": 5,
    }
    assert [UUID(member["agent_id"]) for member in body["members"]] == [
        member.agent_id for member in members
    ]
    assert [version for _seed, version in generator.calls] == [_TARGET] * 5

    async with session_maker() as session:
        rollout = await session.scalar(
            select(BenchmarkRollout).where(BenchmarkRollout.desired_version == _TARGET)
        )
        assert rollout is not None
        positions = list(
            await session.scalars(
                select(BenchmarkRolloutMember.position)
                .where(BenchmarkRolloutMember.rollout_id == rollout.rollout_id)
                .order_by(BenchmarkRolloutMember.position)
            )
        )
        assert positions == list(range(1, 16))
        audit = await session.scalar(
            select(BenchmarkRolloutAudit).where(
                BenchmarkRolloutAudit.rollout_id == rollout.rollout_id,
                BenchmarkRolloutAudit.event == "cohort_expanded",
            )
        )
        assert audit is not None
        assert audit.payload["previous_rescore_cohort_target"] == 10
        assert audit.payload["new_rescore_cohort_target"] == 15
        assert audit.payload["appended_members"] == 5


async def test_expand_open_rollout_refuses_stale_target_guard_without_rendering(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    now = datetime.now(UTC).replace(microsecond=0)
    await _seed_expandable_rollout(session_maker, now)
    generator = _StubGenerator()
    app.state.dataset_generator = generator
    payload = _expand_payload(expected_target=9)

    response = await client.post(
        f"/api/v1/admin/benchmark-rollout/{_TARGET}/expand",
        headers=_HEADERS,
        json=payload,
    )

    assert response.status_code == 409, response.text
    assert "expected 9, found 10" in response.json()["message"]
    assert generator.calls == []
    async with session_maker() as session:
        rollout = await session.scalar(
            select(BenchmarkRollout).where(BenchmarkRollout.desired_version == _TARGET)
        )
        assert rollout is not None
        assert rollout.rescore_cohort_target == 10
        assert rollout.cohort_size == 10


async def test_expand_open_rollout_keeps_target_frozen_when_rendering_fails(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    now = datetime.now(UTC).replace(microsecond=0)
    await _seed_expandable_rollout(session_maker, now)
    generator = _StubGenerator(DataPipelineError(_LAGGING))
    app.state.dataset_generator = generator

    response = await client.post(
        f"/api/v1/admin/benchmark-rollout/{_TARGET}/expand",
        headers=_HEADERS,
        json=_expand_payload(),
    )

    assert response.status_code == 502, response.text
    assert generator.calls
    async with session_maker() as session:
        rollout = await session.scalar(
            select(BenchmarkRollout).where(BenchmarkRollout.desired_version == _TARGET)
        )
        assert rollout is not None
        assert rollout.rescore_cohort_target == 10
        assert rollout.cohort_size == 10
        member_count = await session.scalar(
            select(func.count())
            .select_from(BenchmarkRolloutMember)
            .where(BenchmarkRolloutMember.rollout_id == rollout.rollout_id)
        )
        assert member_count == 10


async def test_start_reports_a_lagging_generate_service_as_a_named_502(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """The generator lags this API by a deploy; that is a 502, never a bare 500."""
    _install(app, session_maker)
    now = datetime.now(UTC).replace(microsecond=0)
    await _seed_start_ready(session_maker, now)
    generator = _StubGenerator(DataPipelineError(_LAGGING))
    app.state.dataset_generator = generator

    response = await client.post(
        f"/api/v1/admin/benchmark-rollout/{_TARGET}",
        headers=_HEADERS,
        json=_start_payload(),
    )

    assert response.status_code == 502, response.text
    body = response.json()
    # A handled HTTPException, not the unhandled-exception envelope the
    # operator got in production.
    assert body["error_code"] == ERROR_CODE_HTTP_EXCEPTION
    assert body["error_code"] != ERROR_CODE_UNHANDLED
    message = body["message"]
    assert f"v{_TARGET}" in message
    assert _LAGGING in message
    assert "generate-service" in message
    # The lag is version-specific, so the call must have asked for the target.
    assert [version for _seed, version in generator.calls] == [_TARGET]
    # Nothing half-started: the failed attempt leaves no rollout behind, so a
    # retry after the generator is deployed is a clean start, not a 409.
    assert await _rollout_count(session_maker) == 0


async def test_reposting_an_existing_rollout_also_502s_on_a_lagging_generator(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Re-POSTing is the natural operator retry, so it must not be the 500 route."""
    _install(app, session_maker)
    now = datetime.now(UTC).replace(microsecond=0)
    members = await _seed_start_ready(session_maker, now)
    async with (
        session_maker() as session,
        retired_era_writes_allowed(session),
        session.begin(),
    ):
        await create_rollout_snapshot(
            session,
            members=members,
            datasets={
                member.agent_id: DatasetPin(
                    seed=index, sha256="c" * 64, run_size="full"
                )
                for index, member in enumerate(members, start=1)
            },
            now=now,
            from_version=2,
            desired_version=_TARGET,
        )
        # A newly risen agent outranks the frozen cohort, so the idempotent
        # refresh has something to render and reaches the generator.
        _add_cohort_agent(session, position=6, composite=0.99, now=now)
    generator = _StubGenerator(DataPipelineError(_LAGGING))
    app.state.dataset_generator = generator

    response = await client.post(
        f"/api/v1/admin/benchmark-rollout/{_TARGET}",
        headers=_HEADERS,
        json=_start_payload(),
    )

    assert response.status_code == 502, response.text
    body = response.json()
    assert body["error_code"] == ERROR_CODE_HTTP_EXCEPTION
    assert body["error_code"] != ERROR_CODE_UNHANDLED
    assert _LAGGING in body["message"]
    assert f"v{_TARGET}" in body["message"]
    assert "generate-service" in body["message"]
    assert [version for _seed, version in generator.calls] == [_TARGET]


async def test_start_still_succeeds_when_the_generator_ships_the_target(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Anti-no-op guard: the 502 wrapper swallows nothing on the happy path."""
    _install(app, session_maker)
    now = datetime.now(UTC).replace(microsecond=0)
    members = await _seed_start_ready(session_maker, now)
    generator = _StubGenerator()
    app.state.dataset_generator = generator

    response = await client.post(
        f"/api/v1/admin/benchmark-rollout/{_TARGET}",
        headers=_HEADERS,
        json=_start_payload(),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "collecting"
    assert body["desired_version"] == _TARGET
    assert body["active_version"] == 7
    assert [version for _seed, version in generator.calls] == [_TARGET] * len(members)
    assert {UUID(member["agent_id"]) for member in body["members"]} == {
        member.agent_id for member in members
    }
    assert await _rollout_count(session_maker) == 1
