from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TypeVar
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.admin_quarantine import AdminBenchmarkQualificationRequest
from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.benchmark_contract import (
    benchmark_contract,
    latest_benchmark_contract,
)
from ditto.api_models.ticket_status import TicketPurpose, TicketStatus
from ditto.api_server.attestation import expected_netuid
from ditto.api_server.benchmark_rollout import (
    ensure_rolling_qualification,
    refresh_rolling_qualification,
    rolling_qualification_blockers,
)
from ditto.api_server.endpoints.admin_benchmark_rollout import (
    AdminRolloutStartRequest,
    AdminRolloutSupersedeRequest,
    _require_rollout_start_capacity,
    get_rollout,
    get_rollout_control,
    start_rollout,
    supersede_rollout,
)
from ditto.api_server.endpoints.admin_quarantine import (
    inspect_benchmark_qualification,
    qualify_benchmark_rollout,
)
from ditto.api_server.inference_routing import (
    AGGREGATE_CALIBRATION_SAMPLES,
    AGGREGATE_PROVIDER,
    aggregate_profile_revision,
    benchmark_model,
)
from ditto.db.models import (
    Agent,
    BenchmarkDataset,
    BenchmarkRollout,
    BenchmarkRolloutAudit,
    BenchmarkRolloutMember,
    EvaluationPayment,
    InferenceProviderRoute,
    InferenceRoutingPolicy,
    OwnerAttestation,
    Score,
    ValidatorHeartbeat,
    ValidatorLeaseAudit,
    ValidatorTicket,
)
from ditto.db.queries.benchmark_rollout import (
    CANARY_BENCH_VERSION,
    DEFAULT_BENCH_VERSION,
    DEFAULT_RESCORE_COHORT_SIZE,
    LEGACY_BENCH_VERSION,
    MIN_DESIRED_AUTHORITY_AGENTS,
    MIN_SCOREABLE_BENCH_VERSION,
    DatasetPin,
    InferenceActivationRequirements,
    RolloutConflictError,
    RolloutSnapshotMember,
    active_bench_version,
    append_rollout_member,
    bind_inference_activation_requirements,
    capable_validator_counts,
    create_rollout_snapshot,
    heartbeat_supports_version,
    historical_rescore_cohort,
    inference_activation_ready,
    issue_rollout_ticket,
    maybe_activate_rollout,
    open_rollout,
    persisted_active_bench_version,
    rolling_top_five,
    rollout_cohort_complete,
    rollout_cohort_score_complete,
    rollout_state,
    select_active_bench_version,
    supersede_open_rollout,
)
from ditto.db.queries.queue_policy_settings import (
    insert_queue_policy_settings_revision,
)
from ditto.db.queries.scores import count_ranked_quorum_agents, list_eligible_ledger
from ditto.db.queries.screening import claim_screening_attempts
from ditto.db.queries.tickets import MAX_ATTEMPTS_PER_VERSION
from ditto.tests.legacy_era import (
    grandfather_active_era,
    retired_era_writes_allowed,
)

pytestmark = pytest.mark.asyncio

_Seeded = TypeVar("_Seeded")


async def test_newest_contract_is_a_target_not_an_activation() -> None:
    # v11 is the newest shipped contract. Shipping it makes it a discoverable
    # rollout target and moves CANARY/CURRENT (discovery metadata); it does not
    # activate it or move weight authority, which stays on the durable ledger.
    contract = benchmark_contract(11)
    assert contract.minimum_screening_policy_version == 9
    assert contract.requires_screened_image is True
    assert latest_benchmark_contract() == contract
    assert CANARY_BENCH_VERSION == 11
    assert DEFAULT_BENCH_VERSION == 2
    assert LEGACY_BENCH_VERSION == 2


async def test_admin_status_read_does_not_start_rollout(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    async with session_maker() as session:
        state = await get_rollout(None, session, "v3")
        assert state == {
            # No activation on record, so the ledger answers the floor. Which
            # era that is is incidental to this test; that the read starts
            # nothing is the subject.
            "active_version": MIN_SCOREABLE_BENCH_VERSION,
            "desired_version": MIN_SCOREABLE_BENCH_VERSION,
            "status": "inactive",
            "capability_bench_version": 3,
            "ranked_quorum_agents": 0,
            "min_ranked_quorum_agents": 5,
            "canary_capable_validator_count": 0,
            "v3_capable_validator_count": 0,
            "current_hybrid_top_five": [],
            "qualification_converged": False,
            "cohort_size": 0,
            "cohort_ready_count": 0,
            # No rollout row exists, so nothing has frozen a target yet.
            "rescore_cohort_target": None,
            "max_rescore_cohort_size": 25,
            "priority_cohort_size": 5,
            "priority_cohort_target": None,
            "priority_complete": False,
            "members": [],
        }
        count = await session.scalar(select(func.count(BenchmarkRollout.rollout_id)))
        assert count == 0

        control = await get_rollout_control(None, session, None)  # type: ignore[arg-type]
        # A target must be both above the active version and at or above the
        # floor. Shipping v8 through v11 makes each discoverable as a target but
        # does not create or activate a rollout.
        assert control["available_target_versions"] == [8, 9, 10, 11]
        contracts = control["contracts"]
        assert isinstance(contracts, list)
        assert [item["version"] for item in contracts] == [
            2,
            3,
            4,
            5,
            6,
            7,
            8,
            9,
            10,
            11,
        ]
        assert control["status"] == "inactive"
        count = await session.scalar(select(func.count(BenchmarkRollout.rollout_id)))
        assert count == 0


def _capabilities(now: datetime) -> tuple[dict, dict]:
    revision = "a" * 40
    capabilities = {
        "screened_images": True,
        "require_screened_image": False,
        "source_build_fallback": True,
        "full_stack_managed": False,
        "stack_updater": False,
        "sandbox_egress_restricted": True,
        "ticket_inference": True,
        "signed_score_quorum": True,
        "executor_isolation": "rootless_dind",
        "scorer_benchmarks": {
            "status": "fresh_verified",
            # Model a current scorer that retains every shipped rollout
            # contract. Individual tests narrow this list when they need to
            # exercise a missing-version boundary.
            "supported_bench_versions": [2, 7, 8, 9, 10, 11],
            "observed_at": int(now.timestamp()),
            "software_version": "1.3.0",
            "source_revision": revision,
            "v7_calibration": {
                "manifest_sha256": "c" * 64,
                "supported_routes": [
                    {
                        "provider": "Groq",
                        "profile_revision": "openrouter-route-test-v1",
                        "model": "openai/gpt-oss-20b",
                    }
                ],
            },
        },
    }
    components = {
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
    }
    stack = {
        "mode": "source",
        "compose_schema": 1,
        "release_descriptor_digest": None,
        "components": components,
    }
    return capabilities, stack


def _add_ready_inference_route(
    session,
    now: datetime,
    *,
    provider: str = "Groq",
    profile_revision: str = "openrouter-route-test-v1",
    calibrated: bool = True,
) -> None:
    session.add(
        InferenceRoutingPolicy(
            model="openai/gpt-oss-20b",
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
            model="openai/gpt-oss-20b",
            provider=provider,
            profile_revision=profile_revision,
            status="healthy",
            calibration_status="eligible" if calibrated else "shadow",
            calibration_tool_accuracy=0.65 if calibrated else None,
            calibration_composite=0.20 if calibrated else None,
            calibration_sample_count=60 if calibrated else 0,
            calibration_manifest_sha256="c" * 64 if calibrated else None,
            ewma_error_rate=0,
            ewma_timeout_rate=0,
            sample_count=60 if calibrated else 0,
            selected_ticket_count=0,
            exploration_ticket_count=0,
            discovered_at=now,
            last_observed_at=now,
            updated_at=now,
        )
    )


def _activation_requirements() -> InferenceActivationRequirements:
    return InferenceActivationRequirements(
        enabled=True,
        provider_key_configured=True,
        model="openai/gpt-oss-20b",
        routing_mode="adaptive",
        reviewed_manifest_sha256="c" * 64,
    )


async def _seed_rollout(
    session, now: datetime, *, desired_version: int = CANARY_BENCH_VERSION
) -> tuple[list[UUID], BenchmarkRollout]:
    bind_inference_activation_requirements(session, _activation_requirements())
    # The activation that makes the inherited v2 era the one in force. Without
    # it ``active_bench_version`` has no durable decision to read and answers
    # the FLOOR -- which is this rollout's own target, so source and target
    # collapse onto one era and every "the ledger has not moved yet" assertion
    # below becomes 7 == 7. See ``grandfather_active_era``.
    await grandfather_active_era(
        session, version=DEFAULT_BENCH_VERSION, now=now - timedelta(days=30)
    )
    _add_ready_inference_route(session, now)
    agent_ids = [uuid4() for _ in range(5)]
    members = []
    pins = {}
    for position, agent_id in enumerate(agent_ids, start=1):
        miner = f"miner-{position}"
        session.add(
            Agent(
                agent_id=agent_id,
                miner_hotkey=miner,
                name=f"agent-{position}",
                sha256=f"{position:x}" * 64,
                status=AgentStatus.SCORED,
                screening_policy_version=9,
                screened_image_sha256=f"{position:x}" * 64,
                screened_image_size_bytes=1024,
                screened_image_id="sha256:" + f"{position:x}" * 64,
                screened_image_ref=f"ditto-screen/{agent_id}:latest",
                screened_image_upload_id=uuid4(),
                screened_image_verified_at=now,
                created_at=now + timedelta(seconds=position),
            )
        )
        members.append(
            RolloutSnapshotMember(
                agent_id=agent_id,
                miner_hotkey=miner,
                composite=1 - position / 100,
            )
        )
        pins[agent_id] = DatasetPin(
            seed=position,
            sha256="c" * 64,
            run_size="full",
        )
        for validator in range(3):
            session.add(
                Score(
                    agent_id=agent_id,
                    bench_version=2,
                    validator_hotkey=f"legacy-{validator}",
                    run_id=f"v2-{position}-{validator}",
                    signature="aa",
                    seed=position,
                    composite=0.5 + position / 100,
                    tool_mean=0.5,
                    memory_mean=0.5,
                    median_ms=1,
                    n=114,
                    details={"bench_version": 2},
                    generated_at=now,
                )
            )
    await session.flush()
    # ``from_version`` lost its default (it was 2, and a silent 2 -> target
    # transition is exactly the shape the floor makes meaningless). This
    # fixture's cohort IS the inherited v2 era -- see ``_seeded_session`` -- so
    # it now says so instead of relying on the default that said it for it.
    rollout = await create_rollout_snapshot(
        session,
        members=members,
        datasets=pins,
        now=now,
        from_version=DEFAULT_BENCH_VERSION,
        desired_version=desired_version,
    )
    capabilities, stack = _capabilities(now)
    for hotkey in ("validator-a", "validator-b", "validator-c"):
        session.add(
            ValidatorHeartbeat(
                validator_hotkey=hotkey,
                software_version="1.0.0",
                protocol_version=12,
                code_digest="d" * 64,
                state="polling",
                first_seen_at=now,
                reported_at=now,
                seen_at=now,
                signature="ab" * 64,
                capabilities=capabilities,
                stack=stack,
                # A v10+ validator always advertises capacity; an absent blob is
                # unreadable evidence and can never justify revoking a lease.
                benchmark_capacity={
                    "configured_slots": 1,
                    "healthy_slots": ["slot-0"],
                    "admission": "accepting",
                    "active": [],
                },
            )
        )
    await session.flush()
    return agent_ids, rollout


@asynccontextmanager
async def _seeded_session(
    session_maker: async_sessionmaker[AsyncSession],
    seed: Callable[[AsyncSession], Awaitable[_Seeded]],
) -> AsyncIterator[tuple[AsyncSession, _Seeded]]:
    """Seed the inherited, retired era, then hand the body a live floor.

    Every rollout exercised in this file starts FROM the inherited v2 era, and
    it has to: a rollout must move the version forward to a contract the
    platform ships, the newest shipped contract is v7, so the source era of any
    rollout that can still be built is beneath the retired-era floor. The
    inherited ledger those rollouts rank, rescore and preempt against is
    therefore made of exactly the grandfathered sub-v7 rows production still
    holds -- there is no renumbering that keeps the transition and clears the
    floor.

    So ``seed`` runs with the floor lifted, in its own transaction, and the
    constraints are back (``NOT VALID``, so the seeded rows are grandfathered)
    before the body starts. The body -- the code actually under test -- runs
    against a live floor, so nothing it writes can slip beneath it unnoticed.
    """
    async with session_maker() as session:
        async with retired_era_writes_allowed(session), session.begin():
            seeded = await seed(session)
        async with session.begin():
            yield session, seeded


@asynccontextmanager
async def _seeded_rollout_session(
    session_maker: async_sessionmaker[AsyncSession], now: datetime
) -> AsyncIterator[tuple[AsyncSession, list[UUID], BenchmarkRollout]]:
    """:func:`_seeded_session` for the common five-agent inherited cohort."""
    async with _seeded_session(session_maker, lambda s: _seed_rollout(s, now)) as (
        session,
        (agent_ids, rollout),
    ):
        yield session, agent_ids, rollout


@asynccontextmanager
async def _inherited_era_session(
    session_maker: async_sessionmaker[AsyncSession], now: datetime
) -> AsyncIterator[tuple[AsyncSession, list[UUID], BenchmarkRollout]]:
    """Like :func:`_seeded_rollout_session`, but the floor stays lifted.

    For the tests whose subject IS the inherited era rather than its cohort:
    they keep building it inside the body -- a submission that only ever scored
    on the source version, or a *live* source-era lease the rollout has to
    preempt, refuse or leave alone. A source-era lease in particular can only
    have been issued before the floor landed, since the ticket trigger refuses
    it now; it is precisely the in-flight sub-v7 state that trigger was written
    to let DRAIN rather than strand. Reproducing either means keeping the floor
    down for the body as well as the seed.
    """
    async with (
        session_maker() as session,
        retired_era_writes_allowed(session),
        session.begin(),
    ):
        agent_ids, rollout = await _seed_rollout(session, now)
        yield session, agent_ids, rollout


async def test_historical_rescore_cohort_fills_from_exactly_two_prior_eras(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    expected_v4: list[UUID] = []
    expected_v3: list[UUID] = []
    # v2-v4 are exactly what this cohort is built out of: the two prior eras
    # a rescore inherits from. They are below the retired-era floor and cannot
    # be renumbered above it without ceasing to be prior eras at all, so they
    # are seeded the way production's are -- grandfathered.
    async with (
        session_maker() as session,
        retired_era_writes_allowed(session),
        session.begin(),
    ):
        for version, count in ((4, 6), (3, 10), (2, 5)):
            for rank in range(count):
                agent_id = uuid4()
                session.add(
                    Agent(
                        agent_id=agent_id,
                        miner_hotkey=f"miner-v{version}-{rank}",
                        name=f"agent-v{version}-{rank}",
                        sha256=f"{version:x}" * 64,
                        status=AgentStatus.SCORED,
                        screening_policy_version=9,
                        created_at=now + timedelta(seconds=version * 100 + rank),
                    )
                )
                for validator in range(3):
                    session.add(
                        Score(
                            agent_id=agent_id,
                            bench_version=version,
                            validator_hotkey=f"validator-{version}-{validator}",
                            run_id=f"run-{version}-{rank}-{validator}",
                            signature="aa",
                            seed=rank,
                            composite=1 - rank / 100,
                            tool_mean=0.5,
                            memory_mean=0.5,
                            median_ms=1,
                            n=114,
                            details={"bench_version": version},
                            generated_at=now,
                        )
                    )
                if version == 4:
                    expected_v4.append(agent_id)
                elif version == 3 and rank < 4:
                    expected_v3.append(agent_id)
        await session.flush()

        cohort = await historical_rescore_cohort(session, source_version=4)
        assert [member.agent_id for member in cohort] == [
            *expected_v4,
            *expected_v3,
        ]
        assert len(cohort) == 10
        assert not any("v2" in member.miner_hotkey for member in cohort)


async def test_historical_rescore_cohort_collapses_attested_owner_family(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Rollout membership uses the same linked-owner roots as the ledger."""
    now = datetime.now(UTC).replace(microsecond=0)
    rows = [
        ("miner-linked-high", "coldkey-linked-high", 0.99),
        ("miner-linked-low", "coldkey-linked-low", 0.98),
        ("miner-independent-1", "coldkey-independent-1", 0.97),
        ("miner-independent-2", "coldkey-independent-2", 0.96),
        ("miner-independent-3", "coldkey-independent-3", 0.95),
        ("miner-independent-4", "coldkey-independent-4", 0.94),
    ]
    agent_ids: list[UUID] = []
    async with (
        session_maker() as session,
        retired_era_writes_allowed(session),
        session.begin(),
    ):
        for index, (hotkey, coldkey, composite) in enumerate(rows):
            agent_id = uuid4()
            agent_ids.append(agent_id)
            session.add(
                Agent(
                    agent_id=agent_id,
                    miner_hotkey=hotkey,
                    name=hotkey,
                    sha256=f"{index + 1:x}" * 64,
                    status=AgentStatus.SCORED,
                    screening_policy_version=9,
                    created_at=now + timedelta(seconds=index),
                )
            )
            session.add(
                EvaluationPayment(
                    block_hash=f"0x{agent_id.hex}",
                    extrinsic_index=0,
                    agent_id=agent_id,
                    miner_hotkey=hotkey,
                    miner_coldkey=coldkey,
                    amount_rao=1,
                    dest_address="destination",
                    timestamp=now,
                )
            )
            for validator in range(3):
                session.add(
                    Score(
                        agent_id=agent_id,
                        bench_version=7,
                        validator_hotkey=f"validator-{validator}",
                        run_id=f"run-{index}-{validator}",
                        signature="aa",
                        seed=index,
                        composite=composite,
                        tool_mean=0.5,
                        memory_mean=0.5,
                        median_ms=1,
                        n=114,
                        details={"bench_version": 7},
                        generated_at=now,
                    )
                )
        session.add(
            OwnerAttestation(
                netuid=expected_netuid(),
                hotkey_lo="miner-linked-high",
                hotkey_hi="miner-linked-low",
                nonce=uuid4(),
                issued_at=now,
                lo_key_kind="hotkey",
                lo_signer="miner-linked-high",
                lo_signature="a" * 128,
                hi_key_kind="hotkey",
                hi_signer="miner-linked-low",
                hi_signature="b" * 128,
            )
        )
        await session.flush()

        cohort = await historical_rescore_cohort(session, source_version=7, limit=5)

        assert [member.agent_id for member in cohort] == [
            agent_ids[0],
            *agent_ids[2:],
        ]


async def test_rollout_uses_tail_when_validator_cannot_advance_priority_five(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    async with _seeded_rollout_session(session_maker, now) as (
        session,
        priority_ids,
        rollout,
    ):
        sixth_id = uuid4()
        session.add(
            Agent(
                agent_id=sixth_id,
                miner_hotkey="miner-sixth",
                name="sixth",
                sha256="e" * 64,
                status=AgentStatus.SCORED,
                screening_policy_version=9,
                screened_image_sha256="e" * 64,
                screened_image_size_bytes=1024,
                screened_image_id="sha256:" + "e" * 64,
                screened_image_ref=f"ditto-screen/{sixth_id}:latest",
                screened_image_upload_id=uuid4(),
                screened_image_verified_at=now,
                created_at=now + timedelta(minutes=1),
            )
        )
        rollout.cohort_size = 6
        assert await append_rollout_member(
            session,
            rollout=rollout,
            member=RolloutSnapshotMember(sixth_id, "miner-sixth", 0.4),
            dataset=DatasetPin(seed=6, sha256="e" * 64, run_size="full"),
            now=now,
        )

        for index, priority_id in enumerate(priority_ids):
            ticket = await issue_rollout_ticket(
                session,
                validator_hotkey="validator-a",
                now=now,
                ttl=timedelta(minutes=90),
            )
            assert ticket is not None and ticket.agent_id == priority_id
            ticket.status = TicketStatus.SCORED
            session.add(
                Score(
                    agent_id=priority_id,
                    bench_version=CANARY_BENCH_VERSION,
                    validator_hotkey="validator-a",
                    run_id=f"priority-a-{index}",
                    signature="aa",
                    seed=index,
                    composite=0.8,
                    tool_mean=0.8,
                    memory_mean=0.8,
                    median_ms=1,
                    n=114,
                    details={
                        "bench_version": CANARY_BENCH_VERSION,
                        "v9_base": {"semantic_gate_factor_bps": 10_000},
                    },
                    generated_at=now,
                )
            )
            await session.flush()

        leaked_outsider_id = uuid4()
        session.add(
            Agent(
                agent_id=leaked_outsider_id,
                miner_hotkey="miner-leaked-outsider",
                name="leaked-outsider",
                sha256="d" * 64,
                status=AgentStatus.SCORED,
                screening_policy_version=9,
                created_at=now + timedelta(minutes=2),
            )
        )
        for validator in range(3):
            session.add(
                Score(
                    agent_id=leaked_outsider_id,
                    bench_version=CANARY_BENCH_VERSION,
                    validator_hotkey=f"outsider-{validator}",
                    run_id=f"outsider-{validator}",
                    signature="cc",
                    seed=1,
                    composite=0.9,
                    tool_mean=0.9,
                    memory_mean=0.9,
                    median_ms=1,
                    n=114,
                    details={
                        "bench_version": CANARY_BENCH_VERSION,
                        "v9_base": {"semantic_gate_factor_bps": 10_000},
                    },
                    generated_at=now,
                )
            )
        await session.flush()
        # An out-of-cohort v5 quorum left by the old fallback cannot count as a
        # substitute for an unfinished inherited leader.
        assert await active_bench_version(session) == 2

        # Validator A has exhausted its legal top-five work, so idling it cannot
        # advance the activation gate. It may score rank six while validators B
        # and C retain first choice of the incomplete priority members.
        tail_ticket = await issue_rollout_ticket(
            session,
            validator_hotkey="validator-a",
            now=now,
            ttl=timedelta(minutes=90),
        )
        assert tail_ticket is not None and tail_ticket.agent_id == sixth_id
        assert await active_bench_version(session) == 2

        for priority_id in priority_ids:
            for hotkey in ("validator-b", "validator-c"):
                session.add(
                    Score(
                        agent_id=priority_id,
                        bench_version=CANARY_BENCH_VERSION,
                        validator_hotkey=hotkey,
                        run_id=f"priority-{priority_id}-{hotkey}",
                        signature="bb",
                        seed=1,
                        composite=0.8,
                        tool_mean=0.8,
                        memory_mean=0.8,
                        median_ms=1,
                        n=114,
                        details={
                            "bench_version": CANARY_BENCH_VERSION,
                            "v9_base": {"semantic_gate_factor_bps": 10_000},
                        },
                        generated_at=now,
                    )
                )
        await session.flush()
        sixth_ticket = await issue_rollout_ticket(
            session,
            validator_hotkey="validator-a",
            now=now,
            ttl=timedelta(minutes=90),
        )
        assert sixth_ticket is not None and sixth_ticket.agent_id == sixth_id
        # The frozen priority five have quorum, so the target becomes
        # authoritative while this valid tail lease continues in place.
        assert await active_bench_version(session) == CANARY_BENCH_VERSION
        assert not await maybe_activate_rollout(session, rollout, now=now)

        for validator in range(3):
            session.add(
                Score(
                    agent_id=sixth_id,
                    bench_version=CANARY_BENCH_VERSION,
                    validator_hotkey=f"tail-validator-{validator}",
                    run_id=f"tail-{validator}",
                    signature="dd",
                    seed=6,
                    composite=0,
                    tool_mean=0,
                    memory_mean=0,
                    median_ms=1,
                    n=114,
                    details={
                        "bench_version": CANARY_BENCH_VERSION,
                        "v9_base": {"semantic_gate_factor_bps": 0},
                    },
                    generated_at=now,
                )
            )
        await session.flush()
        assert await active_bench_version(session) == CANARY_BENCH_VERSION
        assert await maybe_activate_rollout(
            session,
            rollout,
            now=now,
            inference_requirements=_activation_requirements(),
        )


async def test_source_backfill_gate_waits_for_full_inherited_top_ten(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    async with _seeded_rollout_session(session_maker, now) as (
        session,
        agent_ids,
        rollout,
    ):
        for position in range(6, DEFAULT_RESCORE_COHORT_SIZE + 1):
            agent_id = uuid4()
            agent_ids.append(agent_id)
            session.add(
                Agent(
                    agent_id=agent_id,
                    miner_hotkey=f"miner-{position}",
                    name=f"agent-{position}",
                    sha256=f"{position:x}" * 64,
                    status=AgentStatus.SCORED,
                    screening_policy_version=9,
                    screened_image_sha256=f"{position:x}" * 64,
                    screened_image_size_bytes=1024,
                    screened_image_id="sha256:" + f"{position:x}" * 64,
                    screened_image_ref=f"ditto-screen/{agent_id}:latest",
                    screened_image_upload_id=uuid4(),
                    screened_image_verified_at=now,
                    created_at=now + timedelta(seconds=position),
                )
            )
            assert await append_rollout_member(
                session,
                rollout=rollout,
                member=RolloutSnapshotMember(
                    agent_id, f"miner-{position}", 1 - position / 100
                ),
                dataset=DatasetPin(
                    seed=position, sha256=f"{position:x}" * 64, run_size="full"
                ),
                now=now,
            )

        for position, agent_id in enumerate(agent_ids, start=1):
            validator_count = 2 if position == DEFAULT_RESCORE_COHORT_SIZE else 3
            for validator in range(validator_count):
                session.add(
                    Score(
                        agent_id=agent_id,
                        bench_version=CANARY_BENCH_VERSION,
                        validator_hotkey=f"validator-{validator}",
                        run_id=f"desired-{position}-{validator}",
                        signature="aa",
                        seed=position,
                        composite=0.8,
                        tool_mean=0.8,
                        memory_mean=0.8,
                        median_ms=1,
                        n=(15 if position == DEFAULT_RESCORE_COHORT_SIZE else 114),
                        details={
                            "bench_version": CANARY_BENCH_VERSION,
                            "v9_base": {"semantic_gate_factor_bps": 10_000},
                        },
                        generated_at=now,
                    )
                )
        await session.flush()
        assert not await rollout_cohort_complete(
            session, rollout=rollout, cohort_size=DEFAULT_RESCORE_COHORT_SIZE
        )
        assert await rollout_cohort_complete(session, rollout=rollout, cohort_size=5)

        session.add(
            Score(
                agent_id=agent_ids[-1],
                bench_version=CANARY_BENCH_VERSION,
                validator_hotkey="validator-2",
                run_id="desired-10-2",
                signature="aa",
                seed=10,
                composite=0.8,
                tool_mean=0.8,
                memory_mean=0.8,
                median_ms=1,
                n=15,
                details={"bench_version": CANARY_BENCH_VERSION},
                generated_at=now,
            )
        )
        await session.flush()
        # Three smoke-profile rows are a raw quorum but cannot rank and must
        # not open source-era capacity.
        assert not await rollout_cohort_complete(
            session, rollout=rollout, cohort_size=DEFAULT_RESCORE_COHORT_SIZE
        )
        smoke_scores = (
            (
                await session.execute(
                    select(Score).where(
                        Score.agent_id == agent_ids[-1],
                        Score.bench_version == CANARY_BENCH_VERSION,
                    )
                )
            )
            .scalars()
            .all()
        )
        for score in smoke_scores:
            score.n = 114
        await session.flush()
        assert await rollout_cohort_complete(
            session, rollout=rollout, cohort_size=DEFAULT_RESCORE_COHORT_SIZE
        )


async def test_frozen_top_five_barrier_remains_raw_three_of_three(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    async with _seeded_rollout_session(session_maker, now) as (
        session,
        priority_ids,
        rollout,
    ):
        sixth_id = uuid4()
        session.add(
            Agent(
                agent_id=sixth_id,
                miner_hotkey="miner-sixth-smoke",
                name="sixth-smoke",
                sha256="e" * 64,
                status=AgentStatus.SCORED,
                screening_policy_version=9,
                screened_image_sha256="e" * 64,
                screened_image_size_bytes=1024,
                screened_image_id="sha256:" + "e" * 64,
                screened_image_ref=f"ditto-screen/{sixth_id}:latest",
                screened_image_upload_id=uuid4(),
                screened_image_verified_at=now,
                created_at=now + timedelta(minutes=1),
            )
        )
        rollout.cohort_size = 6
        assert await append_rollout_member(
            session,
            rollout=rollout,
            member=RolloutSnapshotMember(sixth_id, "miner-sixth-smoke", 0.4),
            dataset=DatasetPin(seed=6, sha256="e" * 64, run_size="full"),
            now=now,
        )
        for position, agent_id in enumerate(priority_ids, start=1):
            for validator in range(3):
                session.add(
                    Score(
                        agent_id=agent_id,
                        bench_version=CANARY_BENCH_VERSION,
                        validator_hotkey=f"smoke-{validator}",
                        run_id=f"smoke-{position}-{validator}",
                        signature="aa",
                        seed=position,
                        composite=0.8,
                        tool_mean=0.8,
                        memory_mean=0.8,
                        median_ms=1,
                        n=15,
                        details={"bench_version": CANARY_BENCH_VERSION},
                        generated_at=now,
                    )
                )
        await session.flush()

        assert await rollout_cohort_score_complete(
            session, rollout=rollout, cohort_size=5
        )
        assert not await rollout_cohort_complete(
            session, rollout=rollout, cohort_size=5
        )
        ticket = await issue_rollout_ticket(
            session,
            validator_hotkey="validator-a",
            now=now,
            ttl=timedelta(minutes=90),
        )
        assert ticket is not None
        assert ticket.agent_id == sixth_id


async def test_parallel_rollout_slots_stay_distinct_inside_frozen_priority_five(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    async with _seeded_rollout_session(session_maker, now) as (
        session,
        priority_ids,
        _rollout,
    ):
        slot0 = await issue_rollout_ticket(
            session,
            validator_hotkey="validator-a",
            slot_id="slot-0",
            now=now,
            ttl=timedelta(minutes=90),
        )
        slot1 = await issue_rollout_ticket(
            session,
            validator_hotkey="validator-a",
            slot_id="slot-1",
            now=now,
            ttl=timedelta(minutes=90),
        )
        assert slot0 is not None and slot1 is not None
        assert slot0.agent_id != slot1.agent_id
        assert {slot0.agent_id, slot1.agent_id}.issubset(set(priority_ids))


async def test_ath_hold_keeps_frozen_member_scoreable_and_retains_progress(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """An ATH hold pauses authority, not the benchmark competition itself."""
    now = datetime.now(UTC).replace(microsecond=0)
    async with _seeded_rollout_session(session_maker, now) as (
        session,
        priority_ids,
        rollout,
    ):
        held = await session.get(Agent, priority_ids[0])
        assert held is not None
        held.status = AgentStatus.ATH_PENDING_REVIEW
        # Put the rest of the priority cohort one coverage round ahead so the
        # issuer's balanced ordering returns to this member after its clear.
        for index, agent_id in enumerate(priority_ids[1:], start=1):
            session.add(
                Score(
                    agent_id=agent_id,
                    bench_version=rollout.desired_version,
                    validator_hotkey="coverage-a",
                    run_id=f"coverage-a-{index}",
                    signature="aa",
                    seed=index,
                    composite=0.8,
                    tool_mean=0.8,
                    memory_mean=0.8,
                    median_ms=1,
                    n=114,
                    details={"bench_version": rollout.desired_version},
                    generated_at=now,
                )
            )
        await session.flush()

        first = await issue_rollout_ticket(
            session,
            validator_hotkey="validator-a",
            now=now,
            ttl=timedelta(minutes=90),
        )
        assert first is not None
        assert first.agent_id == held.agent_id

        first.status = TicketStatus.SCORED
        session.add(
            Score(
                agent_id=held.agent_id,
                bench_version=rollout.desired_version,
                validator_hotkey="validator-a",
                run_id="held-validator-a",
                signature="aa",
                seed=1,
                composite=0.8,
                tool_mean=0.8,
                memory_mean=0.8,
                median_ms=1,
                n=114,
                details={"bench_version": rollout.desired_version},
                generated_at=now,
            )
        )
        await session.flush()

        # The accepted score does not make a suspected copy authoritative.
        assert await active_bench_version(session) == 2
        assert not await maybe_activate_rollout(session, rollout, now=now)

        # A human clear makes the retained score immediately useful; the next
        # validator continues at 1/3 instead of restarting after the hold.
        held.status = AgentStatus.SCORED
        await session.flush()
        second = await issue_rollout_ticket(
            session,
            validator_hotkey="validator-b",
            now=now,
            ttl=timedelta(minutes=90),
        )
        assert second is not None
        assert second.agent_id == held.agent_id


async def test_five_agents_remain_v2_at_two_of_three_then_activate_atomically(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    async with _inherited_era_session(session_maker, now) as (
        session,
        agent_ids,
        rollout,
    ):
        v2_only_id = uuid4()
        session.add(
            Agent(
                agent_id=v2_only_id,
                miner_hotkey="miner-v2-only",
                name="v2-only",
                sha256="e" * 64,
                status=AgentStatus.SCORED,
                screening_policy_version=9,
                created_at=now + timedelta(minutes=1),
            )
        )
        for validator in range(3):
            session.add(
                Score(
                    agent_id=v2_only_id,
                    bench_version=2,
                    validator_hotkey=f"legacy-{validator}",
                    run_id=f"v2-only-{validator}",
                    signature="dd",
                    seed=99,
                    composite=0.4,
                    tool_mean=0.4,
                    memory_mean=0.4,
                    median_ms=1,
                    n=114,
                    details={"bench_version": 2},
                    generated_at=now,
                )
            )
        await session.flush()
        heartbeat = await session.get(ValidatorHeartbeat, "validator-a")
        assert heartbeat is not None
        assert heartbeat_supports_version(heartbeat, now=now)
        heartbeat.protocol_version = 7
        assert not heartbeat_supports_version(heartbeat, now=now)
        heartbeat.protocol_version = 12

        for validator_index, hotkey in enumerate(("validator-a", "validator-b")):
            for agent_index in range(5):
                ticket = await issue_rollout_ticket(
                    session,
                    validator_hotkey=hotkey,
                    now=now,
                    ttl=timedelta(minutes=90),
                )
                assert ticket is not None
                assert ticket.agent_id == agent_ids[agent_index]
                session.add(
                    Score(
                        agent_id=ticket.agent_id,
                        bench_version=CANARY_BENCH_VERSION,
                        validator_hotkey=hotkey,
                        run_id=f"v3-{validator_index}-{agent_index}",
                        signature="bb",
                        seed=agent_index + 1,
                        composite=0.7 + agent_index / 100,
                        tool_mean=0.7,
                        memory_mean=0.7,
                        median_ms=1,
                        n=114,
                        details={
                            "bench_version": CANARY_BENCH_VERSION,
                            "v9_base": {"semantic_gate_factor_bps": 10_000},
                        },
                        generated_at=now,
                    )
                )
                ticket.status = TicketStatus.SCORED
                await session.flush()

        state = await rollout_state(session)
        assert state["active_version"] == 2
        assert state["v3_capable_validator_count"] == 3
        assert [member["score_count"] for member in state["members"]] == [2] * 5
        # The authority-switch threshold is public: at 2/3 scores per member no
        # agent holds a ranked quorum yet, and the client reads the bar rather
        # than hardcoding it.
        assert state["ranked_quorum_agents"] == 0
        assert state["min_ranked_quorum_agents"] == MIN_DESIRED_AUTHORITY_AGENTS
        assert await active_bench_version(session) == 2
        collecting_ledger = await list_eligible_ledger(session)
        assert {row.agent_id for row in collecting_ledger} == {
            *agent_ids,
            v2_only_id,
        }
        assert all(row.bench_version == 2 for row in collecting_ledger)

        activations = []
        for agent_index in range(5):
            ticket = await issue_rollout_ticket(
                session,
                validator_hotkey="validator-c",
                now=now,
                ttl=timedelta(minutes=90),
            )
            assert ticket is not None
            assert ticket.agent_id == agent_ids[agent_index]
            session.add(
                Score(
                    agent_id=ticket.agent_id,
                    bench_version=CANARY_BENCH_VERSION,
                    validator_hotkey="validator-c",
                    run_id=f"v3-2-{agent_index}",
                    signature="cc",
                    seed=agent_index + 1,
                    composite=0.8 + agent_index / 100,
                    tool_mean=0.8,
                    memory_mean=0.8,
                    median_ms=1,
                    n=114,
                    details={
                        "bench_version": CANARY_BENCH_VERSION,
                        "v9_base": {"semantic_gate_factor_bps": 10_000},
                    },
                    generated_at=now,
                )
            )
            ticket.status = TicketStatus.SCORED
            await session.flush()
            activations.append(
                await maybe_activate_rollout(
                    session,
                    rollout,
                    now=now,
                    inference_requirements=_activation_requirements(),
                )
            )
            if agent_index == 0:
                # Agent 0 has a complete desired-version quorum, but it is one
                # of MIN_DESIRED_AUTHORITY_AGENTS, so the threshold gate keeps
                # the whole ledger on its settled v2 medians.
                pinned = await list_eligible_ledger(session)
                by_agent = {row.agent_id: row for row in pinned}
                assert by_agent[agent_ids[0]].bench_version == 2
                assert by_agent[agent_ids[0]].composite == pytest.approx(0.51)
                assert all(
                    by_agent[agent_id].bench_version == 2 for agent_id in agent_ids[1:]
                )
                assert by_agent[v2_only_id].bench_version == 2

        assert activations == [False, False, False, False, True]
        assert await active_bench_version(session) == CANARY_BENCH_VERSION
        state = await rollout_state(session)
        assert state["status"] == "activated"
        assert [member["score_count"] for member in state["members"]] == [3] * 5
        v3_ledger = await list_eligible_ledger(session)
        assert len(v3_ledger) == 5
        assert v2_only_id not in {row.agent_id for row in v3_ledger}
        assert all(
            row.bench_version == CANARY_BENCH_VERSION
            and row.details is not None
            and row.details["bench_version"] == CANARY_BENCH_VERSION
            for row in v3_ledger
        )


async def test_ineligible_qualified_member_does_not_block_remaining_work(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    async with _seeded_rollout_session(session_maker, now) as (
        session,
        agent_ids,
        _,
    ):
        agent = await session.get(Agent, agent_ids[2])
        assert agent is not None
        agent.status = AgentStatus.BANNED
        ticket = await issue_rollout_ticket(
            session,
            validator_hotkey="validator-a",
            now=now,
            ttl=timedelta(minutes=90),
        )
        assert ticket is not None
        assert ticket.agent_id != agent_ids[2]
        state = await rollout_state(session)
        assert state["status"] == "collecting"
        assert [UUID(member["agent_id"]) for member in state["members"]] == agent_ids
        agent.status = AgentStatus.SCORED
        ticket = await issue_rollout_ticket(
            session,
            validator_hotkey="validator-a",
            now=now,
            ttl=timedelta(minutes=90),
        )
        assert ticket is not None
        assert [
            UUID(member["agent_id"])
            for member in (await rollout_state(session))["members"]
        ] == agent_ids


async def test_rollout_screened_only_skips_and_releases_source_only_work(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    async with _inherited_era_session(session_maker, now) as (
        session,
        agent_ids,
        rollout,
    ):
        for agent_id in agent_ids:
            agent = await session.get(Agent, agent_id)
            assert agent is not None
            agent.screened_image_sha256 = None
            agent.screened_image_size_bytes = None
            agent.screened_image_id = None
            agent.screened_image_ref = None
            agent.screened_image_upload_id = None
            agent.screened_image_verified_at = None
        screened = await session.get(Agent, agent_ids[1])
        assert screened is not None
        screened.screened_image_sha256 = "12" * 32
        screened.screened_image_size_bytes = 123
        screened.screened_image_id = "sha256:" + "34" * 32
        screened.screened_image_ref = f"ditto-screen/{screened.agent_id}:latest"
        screened.screened_image_upload_id = uuid4()
        screened.screened_image_verified_at = now

        first = await issue_rollout_ticket(
            session,
            validator_hotkey="validator-a",
            now=now,
            ttl=timedelta(minutes=90),
            artifact_mode="screened_only",
        )
        assert first is not None
        assert first.agent_id == screened.agent_id

        incompatible = ValidatorTicket(
            agent_id=agent_ids[0],
            bench_version=rollout.desired_version,
            validator_hotkey="validator-b",
            status=TicketStatus.ISSUED,
            # The slot was advertised once and is not any more, which is genuine
            # idleness. Without that first report its silence would just mean the
            # run had not announced itself yet, and the lease would be protected.
            issued_at=now - timedelta(minutes=10),
            deadline=now + timedelta(minutes=90),
            attempt_count=1,
            manual_retry_grants=0,
            first_reported_at=now - timedelta(minutes=9),
        )
        session.add(incompatible)
        await session.flush()
        replacement = await issue_rollout_ticket(
            session,
            validator_hotkey="validator-b",
            now=now,
            ttl=timedelta(minutes=90),
            artifact_mode="screened_only",
        )
        assert replacement is not None
        assert replacement.agent_id == screened.agent_id
        assert incompatible.status == TicketStatus.EXPIRED

        running = ValidatorTicket(
            agent_id=agent_ids[0],
            bench_version=rollout.desired_version,
            validator_hotkey="validator-c",
            status=TicketStatus.ISSUED,
            issued_at=now,
            deadline=now + timedelta(minutes=90),
            attempt_count=1,
            manual_retry_grants=0,
        )
        session.add(running)
        await session.flush()
        assert (
            await issue_rollout_ticket(
                session,
                validator_hotkey="validator-c",
                now=now,
                ttl=timedelta(minutes=90),
                artifact_mode="screened_only",
                validator_running_benchmark=True,
            )
            is None
        )
        assert running.status == TicketStatus.ISSUED


async def test_rollout_ticket_respects_retry_cooldown_and_attempt_cap(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    async with _inherited_era_session(session_maker, now) as (
        session,
        agent_ids,
        _rollout,
    ):
        exhausted = await issue_rollout_ticket(
            session,
            validator_hotkey="validator-a",
            now=now,
            ttl=timedelta(minutes=90),
        )
        assert exhausted is not None and exhausted.agent_id == agent_ids[0]
        exhausted.status = TicketStatus.EXPIRED
        exhausted.deadline = now
        exhausted.retry_after = now
        exhausted.attempt_count = MAX_ATTEMPTS_PER_VERSION

        cooling = await issue_rollout_ticket(
            session,
            validator_hotkey="validator-a",
            now=now,
            ttl=timedelta(minutes=90),
        )
        assert cooling is not None and cooling.agent_id == agent_ids[1]
        cooling.status = TicketStatus.EXPIRED
        cooling.deadline = now
        cooling.retry_after = now + timedelta(minutes=10)

        replacement = await issue_rollout_ticket(
            session,
            validator_hotkey="validator-a",
            now=now,
            ttl=timedelta(minutes=90),
        )
        assert replacement is not None and replacement.agent_id == agent_ids[2]

        # An audited grant makes the first ticket eligible again; the rollout
        # lane must use the same cap arithmetic as ordinary ticket issuance.
        exhausted.manual_retry_grants = 1
        granted = await issue_rollout_ticket(
            session,
            validator_hotkey="validator-a",
            now=now,
            ttl=timedelta(minutes=90),
            slot_id="slot-1",
        )
        assert granted is exhausted
        assert granted.status == TicketStatus.ISSUED
        assert granted.attempt_count == MAX_ATTEMPTS_PER_VERSION + 1


async def test_rollout_preempts_idle_source_lease_only_when_target_work_exists(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    async with _inherited_era_session(session_maker, now) as (
        session,
        agent_ids,
        rollout,
    ):
        ordinary_id = uuid4()
        session.add(
            Agent(
                agent_id=ordinary_id,
                miner_hotkey="ordinary-miner",
                name="ordinary-v2-work",
                sha256="ab" * 32,
                status=AgentStatus.EVALUATING,
                screening_policy_version=9,
                created_at=now + timedelta(minutes=1),
            )
        )
        idle_source_ticket = ValidatorTicket(
            agent_id=ordinary_id,
            bench_version=2,
            validator_hotkey="validator-a",
            status=TicketStatus.ISSUED,
            # Reported once and now absent, so validator-a's empty capacity blob
            # is evidence the slot really is idle rather than merely starting up.
            issued_at=now - timedelta(minutes=10),
            deadline=now + timedelta(minutes=90),
            attempt_count=1,
            manual_retry_grants=0,
            first_reported_at=now - timedelta(minutes=9),
        )
        running_source_ticket = ValidatorTicket(
            agent_id=ordinary_id,
            bench_version=2,
            validator_hotkey="validator-b",
            status=TicketStatus.ISSUED,
            issued_at=now,
            deadline=now + timedelta(minutes=90),
            attempt_count=1,
            manual_retry_grants=0,
        )
        no_target_source_ticket = ValidatorTicket(
            agent_id=ordinary_id,
            bench_version=2,
            validator_hotkey="validator-c",
            status=TicketStatus.ISSUED,
            issued_at=now,
            deadline=now + timedelta(minutes=90),
            attempt_count=1,
            manual_retry_grants=0,
        )
        session.add_all(
            [idle_source_ticket, running_source_ticket, no_target_source_ticket]
        )
        await session.flush()

        replacement = await issue_rollout_ticket(
            session,
            validator_hotkey="validator-a",
            now=now,
            ttl=timedelta(minutes=90),
        )
        assert replacement is not None
        assert replacement.agent_id == agent_ids[0]
        assert replacement.bench_version == rollout.desired_version
        assert idle_source_ticket.status == TicketStatus.EXPIRED

        expired_deadline_id = uuid4()
        session.add(
            Agent(
                agent_id=expired_deadline_id,
                miner_hotkey="expired-deadline-miner",
                name="expired-deadline-v2-work",
                sha256="bc" * 32,
                status=AgentStatus.EVALUATING,
                screening_policy_version=9,
                created_at=now + timedelta(minutes=2),
            )
        )
        stale_issued_ticket = ValidatorTicket(
            agent_id=expired_deadline_id,
            bench_version=2,
            validator_hotkey="validator-d",
            status=TicketStatus.ISSUED,
            issued_at=now - timedelta(minutes=90),
            deadline=now - timedelta(seconds=1),
            attempt_count=1,
            manual_retry_grants=0,
        )
        session.add(stale_issued_ticket)
        capabilities, stack = _capabilities(now)
        session.add(
            ValidatorHeartbeat(
                validator_hotkey="validator-d",
                software_version="1.0.0",
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
        await session.flush()
        after_stale_deadline = await issue_rollout_ticket(
            session,
            validator_hotkey="validator-d",
            now=now,
            ttl=timedelta(minutes=90),
        )
        assert after_stale_deadline is not None
        assert after_stale_deadline.bench_version == rollout.desired_version
        assert stale_issued_ticket.status == TicketStatus.EXPIRED

        assert (
            await issue_rollout_ticket(
                session,
                validator_hotkey="validator-b",
                now=now,
                ttl=timedelta(minutes=90),
                validator_running_benchmark=True,
            )
            is None
        )
        assert running_source_ticket.status == TicketStatus.ISSUED

        for agent_id in agent_ids:
            agent = await session.get(Agent, agent_id)
            assert agent is not None
            agent.screened_image_sha256 = None
            agent.screened_image_size_bytes = None
            agent.screened_image_id = None
            agent.screened_image_ref = None
            agent.screened_image_upload_id = None
            agent.screened_image_verified_at = None
        await session.flush()
        assert (
            await issue_rollout_ticket(
                session,
                validator_hotkey="validator-c",
                now=now,
                ttl=timedelta(minutes=90),
            )
            is None
        )
        assert no_target_source_ticket.status == TicketStatus.ISSUED


async def test_rollout_never_preempts_a_lease_behind_a_frozen_capacity_blob(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """The rollout lane carried the looser copy of the revocation. A validator
    whose heartbeat ingest has stalled must keep its in-flight lease here too --
    the blob is a cache of the last successful ingest, not a liveness probe."""
    now = datetime.now(UTC).replace(microsecond=0)
    async with _inherited_era_session(session_maker, now) as (
        session,
        _,
        rollout,
    ):
        ordinary_id = uuid4()
        session.add(
            Agent(
                agent_id=ordinary_id,
                miner_hotkey="ordinary-miner",
                name="ordinary-v2-work",
                sha256="ab" * 32,
                status=AgentStatus.EVALUATING,
                screening_policy_version=9,
                created_at=now + timedelta(minutes=1),
            )
        )
        live_source_ticket = ValidatorTicket(
            agent_id=ordinary_id,
            bench_version=2,
            validator_hotkey="validator-a",
            status=TicketStatus.ISSUED,
            issued_at=now - timedelta(minutes=19),
            deadline=now + timedelta(minutes=71),
            attempt_count=1,
            manual_retry_grants=0,
        )
        session.add(live_source_ticket)
        heartbeat = await session.get(ValidatorHeartbeat, "validator-a")
        assert heartbeat is not None
        # Four minutes without a successful ingest: still inside the five-minute
        # window every other gate uses, so the validator is served work as
        # normal, but far too old to prove a slot is empty. This is the window
        # the destroyed v7 runs died in.
        heartbeat.seen_at = now - timedelta(minutes=4)
        await session.flush()

        assert (
            await issue_rollout_ticket(
                session,
                validator_hotkey="validator-a",
                now=now,
                ttl=timedelta(minutes=90),
            )
            is None
        )
        assert live_source_ticket.status == TicketStatus.ISSUED
        assert live_source_ticket.deadline.replace(tzinfo=UTC) == now + timedelta(
            minutes=71
        )
        assert (
            await session.scalar(select(func.count()).select_from(ValidatorLeaseAudit))
        ) == 0


@pytest.mark.parametrize(
    "purpose",
    [TicketPurpose.CONTINUAL_RETEST, TicketPurpose.LEGACY_UNCLASSIFIED],
)
async def test_rollout_does_not_serve_or_preempt_noncanonical_lease(
    session_maker: async_sessionmaker[AsyncSession],
    purpose: TicketPurpose,
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    async with _inherited_era_session(session_maker, now) as (
        session,
        _,
        rollout,
    ):
        ordinary_id = uuid4()
        session.add(
            Agent(
                agent_id=ordinary_id,
                miner_hotkey=f"ordinary-{purpose}",
                name=f"ordinary-{purpose}",
                sha256="fa" * 32,
                status=AgentStatus.EVALUATING,
                screening_policy_version=9,
                created_at=now,
            )
        )
        lease = ValidatorTicket(
            agent_id=ordinary_id,
            bench_version=2,
            validator_hotkey="validator-a",
            status=TicketStatus.ISSUED,
            purpose=purpose,
            purpose_revision=(0 if purpose == TicketPurpose.LEGACY_UNCLASSIFIED else 1),
            issued_at=now,
            deadline=now + timedelta(minutes=90),
        )
        session.add(lease)
        await session.flush()

        issued = await issue_rollout_ticket(
            session,
            validator_hotkey="validator-a",
            now=now,
            ttl=timedelta(minutes=90),
        )

        assert issued is None
        assert lease.status == TicketStatus.ISSUED
        assert lease.bench_version != rollout.desired_version


async def test_v3_score_drop_qualifies_and_rescreens_new_top_five_agent(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    rising_id = uuid4()
    async with session_maker() as session:
        # The rising sixth is seeded on the inherited v2 ledger, the era the
        # rollout is migrating away from -- below the retired-era floor by
        # construction, since the only shipped target above it is v7. The
        # floor is restored before the qualification under test runs.
        async with retired_era_writes_allowed(session), session.begin():
            initial_ids, rollout = await _seed_rollout(session, now)
            session.add(
                Agent(
                    agent_id=rising_id,
                    miner_hotkey="miner-6",
                    name="rising-sixth",
                    sha256="f" * 64,
                    status=AgentStatus.SCORED,
                    screening_policy_version=8,
                    dataset_seed=66,
                    dataset_sha256="d" * 64,
                    dataset_run_size="full",
                    created_at=now + timedelta(minutes=1),
                )
            )
            for validator in range(3):
                session.add(
                    Score(
                        agent_id=rising_id,
                        bench_version=2,
                        validator_hotkey=f"legacy-{validator}",
                        run_id=f"v2-rising-{validator}",
                        signature="aa",
                        seed=66,
                        composite=0.505,
                        tool_mean=0.5,
                        memory_mean=0.5,
                        median_ms=1,
                        n=114,
                        details={"bench_version": 2},
                        generated_at=now,
                    )
                )
                session.add(
                    Score(
                        agent_id=initial_ids[0],
                        bench_version=CANARY_BENCH_VERSION,
                        validator_hotkey=f"v3-{validator}",
                        run_id=f"v3-drop-{validator}",
                        signature="bb",
                        seed=1,
                        composite=0.1,
                        tool_mean=0.1,
                        memory_mean=0.1,
                        median_ms=1,
                        n=114,
                        details={"bench_version": CANARY_BENCH_VERSION},
                        generated_at=now,
                    )
                )

        generator = AsyncMock()
        generator.generate.return_value = "e" * 64
        assert (
            await refresh_rolling_qualification(
                session, generator=generator, now=now + timedelta(seconds=1)
            )
            == 1
        )
        async with session.begin():
            member = await session.get(
                BenchmarkRolloutMember, (rollout.rollout_id, rising_id)
            )
            assert member is not None
            assert member.position == 6
            claimed = await claim_screening_attempts(
                session,
                screener_hotkey="screener-1",
                now=now + timedelta(seconds=2),
                ttl=timedelta(minutes=70),
                limit=20,
            )
            assert rising_id in {agent.agent_id for agent, _attempt, _dup in claimed}
            rising = await session.get(Agent, rising_id)
            assert rising is not None
            assert rising.status == AgentStatus.SCORED


async def test_legacy_scored_top_five_recovers_seed_and_converges_idempotently(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    rising_id = uuid4()
    async with session_maker() as session:
        # The rising sixth is seeded on the inherited v2 ledger, the era the
        # rollout is migrating away from -- below the retired-era floor by
        # construction, since the only shipped target above it is v7. The
        # floor is restored before the qualification under test runs.
        async with retired_era_writes_allowed(session), session.begin():
            initial_ids, rollout = await _seed_rollout(session, now)
            session.add(
                Agent(
                    agent_id=rising_id,
                    miner_hotkey="miner-legacy",
                    name="legacy-riser",
                    sha256="f" * 64,
                    status=AgentStatus.SCORED,
                    screening_policy_version=8,
                    created_at=now + timedelta(minutes=1),
                )
            )
            for validator in range(3):
                session.add(
                    Score(
                        agent_id=rising_id,
                        bench_version=2,
                        validator_hotkey=f"legacy-riser-{validator}",
                        run_id=f"legacy-riser-{validator}",
                        signature="aa",
                        seed=8675309,
                        composite=0.505,
                        tool_mean=0.5,
                        memory_mean=0.5,
                        median_ms=1,
                        n=114,
                        details={"bench_version": 2},
                        generated_at=now,
                    )
                )
                session.add(
                    Score(
                        agent_id=initial_ids[0],
                        bench_version=CANARY_BENCH_VERSION,
                        validator_hotkey=f"drop-{validator}",
                        run_id=f"drop-{validator}",
                        signature="bb",
                        seed=1,
                        composite=0.1,
                        tool_mean=0.1,
                        memory_mean=0.1,
                        median_ms=1,
                        n=114,
                        details={"bench_version": CANARY_BENCH_VERSION},
                        generated_at=now,
                    )
                )

        async def generate(seed: int, *, bench_version: int) -> str:
            assert not session.in_transaction()
            assert seed == 8675309
            assert bench_version == CANARY_BENCH_VERSION
            return "e" * 64

        generator = AsyncMock()
        generator.run_size = "full"
        generator.generate.side_effect = generate
        assert (
            await refresh_rolling_qualification(
                session, generator=generator, now=now + timedelta(seconds=1)
            )
            == 1
        )
        assert (
            await refresh_rolling_qualification(
                session, generator=generator, now=now + timedelta(seconds=2)
            )
            == 0
        )
        assert generator.generate.await_count == 1
        async with session.begin():
            member = await session.get(
                BenchmarkRolloutMember, (rollout.rollout_id, rising_id)
            )
            dataset = await session.get(
                BenchmarkDataset, (rising_id, CANARY_BENCH_VERSION)
            )
            legacy = await session.get(Agent, rising_id)
            assert member is not None
            assert dataset is not None
            assert dataset.seed == 8675309
            assert dataset.sha256 == "e" * 64
            assert dataset.run_size == "full"
            assert dataset.seed_block is None
            assert legacy is not None
            assert legacy.dataset_seed is None
            assert legacy.dataset_sha256 is None
            claimed = await claim_screening_attempts(
                session,
                screener_hotkey="screener-legacy",
                now=now + timedelta(seconds=3),
                ttl=timedelta(minutes=70),
                limit=20,
            )
            assert rising_id in {agent.agent_id for agent, _attempt, _dup in claimed}
            assert legacy.status == AgentStatus.SCORED
            rising_attempt = next(
                attempt
                for agent, attempt, _dup in claimed
                if agent.agent_id == rising_id
            )
            rising_attempt.status = "passed"
            rising_attempt.finished_at = now + timedelta(seconds=4)
            legacy.screening_policy_version = 9
            legacy.screened_image_sha256 = "1" * 64
            legacy.screened_image_size_bytes = 1024
            legacy.screened_image_id = "sha256:" + "2" * 64
            legacy.screened_image_ref = f"ditto-screen/{rising_id}:latest"
            legacy.screened_image_upload_id = uuid4()
            legacy.screened_image_verified_at = now + timedelta(seconds=4)
            for initial_id in initial_ids[1:]:
                for validator in range(3):
                    session.add(
                        Score(
                            agent_id=initial_id,
                            bench_version=CANARY_BENCH_VERSION,
                            validator_hotkey=f"filled-{initial_id}-{validator}",
                            run_id=f"filled-{initial_id}-{validator}",
                            signature="cc",
                            seed=1,
                            composite=0.1,
                            tool_mean=0.1,
                            memory_mean=0.1,
                            median_ms=1,
                            n=114,
                            details={"bench_version": CANARY_BENCH_VERSION},
                            generated_at=now,
                        )
                    )
            await session.flush()
            ticket = await issue_rollout_ticket(
                session,
                validator_hotkey="validator-a",
                now=now + timedelta(seconds=5),
                ttl=timedelta(minutes=90),
            )
            assert ticket is not None
            assert ticket.agent_id == rising_id
            assert ticket.bench_version == CANARY_BENCH_VERSION


async def test_admin_qualifies_scored_top_five_with_compare_and_swap_guards(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    rising_id = uuid4()
    async with session_maker() as session:
        # The rising sixth is seeded on the inherited v2 ledger, the era the
        # rollout is migrating away from -- below the retired-era floor by
        # construction, since the only shipped target above it is v7. The
        # floor is restored before the qualification under test runs.
        async with retired_era_writes_allowed(session), session.begin():
            initial_ids, rollout = await _seed_rollout(session, now)
            session.add(
                Agent(
                    agent_id=rising_id,
                    miner_hotkey="miner-admin-riser",
                    name="admin-riser",
                    sha256="f" * 64,
                    status=AgentStatus.SCORED,
                    screening_policy_version=8,
                    created_at=now + timedelta(minutes=1),
                )
            )
            for validator, seed in enumerate((43, 41, 42)):
                session.add(
                    Score(
                        agent_id=rising_id,
                        bench_version=2,
                        validator_hotkey=f"admin-riser-{validator}",
                        run_id=f"admin-riser-{validator}",
                        signature="aa",
                        seed=seed,
                        composite=0.505,
                        tool_mean=0.5,
                        memory_mean=0.5,
                        median_ms=1,
                        n=114,
                        details={"bench_version": 2},
                        generated_at=now,
                    )
                )
                session.add(
                    Score(
                        agent_id=initial_ids[0],
                        bench_version=CANARY_BENCH_VERSION,
                        validator_hotkey=f"admin-drop-{validator}",
                        run_id=f"admin-drop-{validator}",
                        signature="bb",
                        seed=1,
                        composite=0.1,
                        tool_mean=0.1,
                        memory_mean=0.1,
                        median_ms=1,
                        n=114,
                        details={"bench_version": CANARY_BENCH_VERSION},
                        generated_at=now,
                    )
                )

        rollout_id = rollout.rollout_id
        generator = AsyncMock()
        generator.run_size = "full"
        generator.generate.return_value = "e" * 64
        detail = await inspect_benchmark_qualification(
            rising_id, None, session, generator
        )
        assert detail.qualification_allowed
        assert detail.currently_top_five
        assert not detail.rollout_member
        assert detail.total_score_count == 3
        assert detail.source_score_count == 3
        assert detail.target_score_count == 0

        await session.rollback()
        # A live source-era lease, which the ticket trigger now refuses to
        # create -- it is by definition a lease that predates the floor. The
        # floor goes straight back on, and the deadline rewrite below still
        # runs under it: the trigger deliberately permits a retired lease to
        # DRAIN, refusing only its re-issue.
        async with retired_era_writes_allowed(session), session.begin():
            issued = ValidatorTicket(
                agent_id=rising_id,
                bench_version=2,
                validator_hotkey="validator-issued-before-heartbeat",
                status=TicketStatus.ISSUED,
                issued_at=now,
                deadline=now + timedelta(minutes=30),
                attempt_count=1,
                manual_retry_grants=0,
            )
            session.add(issued)
        blocked = await inspect_benchmark_qualification(
            rising_id, None, session, generator
        )
        assert blocked.validator_run_active
        assert blocked.blocking_reason == "validator benchmark is active"
        await session.rollback()
        async with session.begin():
            locked_issued = await session.get(
                ValidatorTicket,
                (rising_id, 2, "validator-issued-before-heartbeat"),
            )
            assert locked_issued is not None
            locked_issued.deadline = now - timedelta(seconds=1)

        with pytest.raises(HTTPException, match="score count changed"):
            await qualify_benchmark_rollout(
                rising_id,
                AdminBenchmarkQualificationRequest(
                    reason="recover legacy top-five qualification",
                    expected_sha256="f" * 64,
                    expected_rollout_id=rollout_id,
                    expected_total_score_count=4,
                    expected_source_score_count=3,
                    expected_target_score_count=0,
                ),
                None,
                session,
                generator,
                "backroom:test",
            )

        response = await qualify_benchmark_rollout(
            rising_id,
            AdminBenchmarkQualificationRequest(
                reason="recover legacy top-five qualification",
                expected_sha256="f" * 64,
                expected_rollout_id=rollout_id,
                expected_total_score_count=3,
                expected_source_score_count=3,
                expected_target_score_count=0,
            ),
            None,
            session,
            generator,
            "backroom:test",
        )
        assert response.agent_status == AgentStatus.SCORED
        assert response.rollout_member
        assert response.screening_queued
        assert response.target_dataset_sha256 == "e" * 64
        async with session.begin():
            scores = list(
                await session.scalars(select(Score).where(Score.agent_id == rising_id))
            )
            agent = await session.get(Agent, rising_id)
            dataset = await session.get(
                BenchmarkDataset, (rising_id, CANARY_BENCH_VERSION)
            )
            audit = await session.scalar(
                select(BenchmarkRolloutAudit).where(
                    BenchmarkRolloutAudit.rollout_id == rollout_id,
                    BenchmarkRolloutAudit.event == "member_qualified",
                )
            )
            assert len(scores) == 3
            assert agent is not None and agent.status == AgentStatus.SCORED
            assert dataset is not None and dataset.seed == 41
            assert audit is not None
            assert audit.payload["origin"] == "manual"
            assert audit.payload["actor"] == "backroom:test"
            assert audit.payload["reason"] == ("recover legacy top-five qualification")
            assert audit.payload["seed_source"] == "source_scores_canonical_min"
            assert audit.payload["dataset_seed"] == 41
            assert audit.payload["dataset_sha256"] == "e" * 64


async def test_multiple_legacy_score_seeds_use_canonical_minimum(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    rising_id = uuid4()
    async with session_maker() as session:
        # The rising sixth is seeded on the inherited v2 ledger, the era the
        # rollout is migrating away from -- below the retired-era floor by
        # construction, since the only shipped target above it is v7. The
        # floor is restored before the qualification under test runs.
        async with retired_era_writes_allowed(session), session.begin():
            initial_ids, rollout = await _seed_rollout(session, now)
            session.add(
                Agent(
                    agent_id=rising_id,
                    miner_hotkey="miner-ambiguous",
                    name="ambiguous-riser",
                    sha256="f" * 64,
                    status=AgentStatus.SCORED,
                    screening_policy_version=8,
                    created_at=now + timedelta(minutes=1),
                )
            )
            for validator, seed in enumerate((41, 42, 42)):
                session.add(
                    Score(
                        agent_id=rising_id,
                        bench_version=2,
                        validator_hotkey=f"ambiguous-{validator}",
                        run_id=f"ambiguous-{validator}",
                        signature="aa",
                        seed=seed,
                        composite=0.505,
                        tool_mean=0.5,
                        memory_mean=0.5,
                        median_ms=1,
                        n=114,
                        details={"bench_version": 2},
                        generated_at=now,
                    )
                )
                session.add(
                    Score(
                        agent_id=initial_ids[0],
                        bench_version=CANARY_BENCH_VERSION,
                        validator_hotkey=f"ambiguous-drop-{validator}",
                        run_id=f"ambiguous-drop-{validator}",
                        signature="bb",
                        seed=1,
                        composite=0.1,
                        tool_mean=0.1,
                        memory_mean=0.1,
                        median_ms=1,
                        n=114,
                        details={"bench_version": CANARY_BENCH_VERSION},
                        generated_at=now,
                    )
                )

        generator = AsyncMock()
        generator.run_size = "full"
        generator.generate.return_value = "e" * 64
        assert (
            await refresh_rolling_qualification(
                session, generator=generator, now=now + timedelta(seconds=1)
            )
            == 1
        )
        generator.generate.assert_awaited_once_with(
            41, bench_version=CANARY_BENCH_VERSION
        )
        blockers = await rolling_qualification_blockers(
            session, generator_run_size="full"
        )
        assert blockers == []
        member = await session.get(
            BenchmarkRolloutMember, (rollout.rollout_id, rising_id)
        )
        dataset = await session.get(BenchmarkDataset, (rising_id, CANARY_BENCH_VERSION))
        audit = await session.scalar(
            select(BenchmarkRolloutAudit).where(
                BenchmarkRolloutAudit.rollout_id == rollout.rollout_id,
                BenchmarkRolloutAudit.event == "member_qualified",
                BenchmarkRolloutAudit.payload["agent_id"].as_string() == str(rising_id),
            )
        )
        assert member is not None
        assert dataset is not None and dataset.seed == 41
        assert audit is not None
        assert audit.payload["origin"] == "automatic"
        assert audit.payload["seed_source"] == "source_scores_canonical_min"
        assert audit.payload["dataset_seed"] == 41
        assert audit.payload["dataset_sha256"] == "e" * 64


async def test_only_one_open_rollout_across_collecting_and_blocked_states(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    async with session_maker() as session:
        session.add_all(
            [
                BenchmarkRollout(
                    rollout_id=uuid4(),
                    from_version=2,
                    desired_version=CANARY_BENCH_VERSION,
                    status="collecting",
                    cohort_size=5,
                ),
                BenchmarkRollout(
                    rollout_id=uuid4(),
                    from_version=2,
                    desired_version=CANARY_BENCH_VERSION,
                    status="blocked_ineligible",
                    cohort_size=5,
                ),
            ]
        )
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()


async def test_capable_validator_cannot_automatically_seed_rollout_work(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    # The inherited v2 ledger a capable validator must NOT be able to turn into
    # rollout work on its own. Retired, so it is seeded with the floor lifted;
    # nothing in the body writes, which is the point of the test.
    async with (
        session_maker() as session,
        retired_era_writes_allowed(session),
        session.begin(),
    ):
        # This test's subject is a PERMISSION distinction -- the validator
        # cannot seed rollout work, the operator can start the rollout the
        # validator could not -- and its second half needs a legal forward
        # target to exercise that. ``CANARY_BENCH_VERSION`` and the floor are
        # both 7, so from == target == 7 violates forward-only and there is no
        # active-era form of this test. Putting the inherited v2 era genuinely
        # in force keeps the 2 -> 7 start legal, on a grandfathered pre-floor
        # row the database legitimately holds.
        #
        # Re-point at a 7 -> 8 rollout once the v8 contract lands
        # (ditto-assistant/ditto-platform#513); only then does a forward target
        # above the floor exist.
        await grandfather_active_era(
            session, version=DEFAULT_BENCH_VERSION, now=now - timedelta(days=30)
        )
        for position in range(1, 6):
            agent_id = uuid4()
            session.add(
                Agent(
                    agent_id=agent_id,
                    miner_hotkey=f"miner-auto-{position}",
                    name=f"auto-{position}",
                    sha256=f"{position:x}" * 64,
                    status=AgentStatus.SCORED,
                    screening_policy_version=9,
                    screened_image_sha256=f"{position:x}" * 64,
                    screened_image_size_bytes=1024,
                    screened_image_id="sha256:" + f"{position:x}" * 64,
                    screened_image_ref=f"ditto-screen/{agent_id}:latest",
                    screened_image_upload_id=uuid4(),
                    screened_image_verified_at=now,
                    dataset_seed=position,
                    dataset_sha256="c" * 64,
                    dataset_run_size="full",
                    created_at=now + timedelta(seconds=position),
                )
            )
            for validator in range(3):
                session.add(
                    Score(
                        agent_id=agent_id,
                        bench_version=2,
                        validator_hotkey=f"legacy-{validator}",
                        run_id=f"auto-v2-{position}-{validator}",
                        signature="aa",
                        seed=position,
                        composite=0.5 + position / 100,
                        tool_mean=0.5,
                        memory_mean=0.5,
                        median_ms=1,
                        n=114,
                        details={"bench_version": 2},
                        generated_at=now,
                    )
                )
        capabilities, stack = _capabilities(now)
        session.add(
            ValidatorHeartbeat(
                validator_hotkey="validator-auto",
                software_version="1.0.0",
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
        _add_ready_inference_route(session, now)

    generator = AsyncMock()
    generator.generate.return_value = "e" * 64
    assert not await ensure_rolling_qualification(session, generator=generator, now=now)
    generator.generate.assert_not_awaited()
    async with session.begin():
        rollout = await open_rollout(session)
        assert rollout is None
        ticket = await issue_rollout_ticket(
            session,
            validator_hotkey="validator-auto",
            now=now,
            ttl=timedelta(minutes=90),
        )
        assert ticket is None

    # Repeated job polls remain fail-closed and cannot render or open a rollout.
    assert not await ensure_rolling_qualification(session, generator=generator, now=now)
    generator.generate.assert_not_awaited()

    state = await start_rollout(
        None,
        session,
        generator,
        str(CANARY_BENCH_VERSION),
        AdminRolloutStartRequest(
            reason="operator opens shipped benchmark",
            actor="backroom:test",
            confirmation=f"START BENCHMARK V{CANARY_BENCH_VERSION}",
            expected_active_version=2,
        ),
    )
    assert state["status"] == "collecting"
    assert state["active_version"] == 2
    assert state["desired_version"] == CANARY_BENCH_VERSION
    assert generator.generate.await_count == 5
    await session.rollback()
    async with session.begin():
        rollout = await open_rollout(session)
        assert rollout is not None
        audit = await session.scalar(
            select(BenchmarkRolloutAudit).where(
                BenchmarkRolloutAudit.rollout_id == rollout.rollout_id,
                BenchmarkRolloutAudit.event == "cohort_frozen",
            )
        )
        assert audit is not None
        assert audit.payload["actor"] == "backroom:test"
        assert audit.payload["reason"] == "operator opens shipped benchmark"
        assert set(audit.payload["seed_sources"].values()) == {"legacy_pin"}


async def test_admin_start_is_idempotent_after_unique_transition_activation(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    rollout_id = uuid4()
    async with session_maker() as session, session.begin():
        session.add(
            BenchmarkRollout(
                rollout_id=rollout_id,
                from_version=2,
                desired_version=CANARY_BENCH_VERSION,
                status="activated",
                cohort_size=5,
                created_at=now,
                activated_at=now,
            )
        )
    async with session_maker() as session:
        with pytest.raises(HTTPException, match="type .* exactly"):
            await start_rollout(
                None,
                session,
                object(),  # type: ignore[arg-type]
                str(CANARY_BENCH_VERSION),
                AdminRolloutStartRequest(
                    reason="confirmation guard",
                    actor="test",
                    confirmation="START BENCHMARK V3",
                    expected_active_version=CANARY_BENCH_VERSION,
                ),
            )
        state = await start_rollout(
            None,
            session,
            object(),  # type: ignore[arg-type]
            str(CANARY_BENCH_VERSION),
            AdminRolloutStartRequest(
                reason="idempotence check",
                actor="test",
                confirmation=f"START BENCHMARK V{CANARY_BENCH_VERSION}",
                expected_active_version=CANARY_BENCH_VERSION,
            ),
        )
        assert state["active_version"] == CANARY_BENCH_VERSION
        assert state["desired_version"] == CANARY_BENCH_VERSION
        assert state["status"] == "activated"
        count = await session.scalar(select(func.count(BenchmarkRollout.rollout_id)))
        assert count == 1
        session.add(
            BenchmarkRollout(
                rollout_id=uuid4(),
                from_version=2,
                desired_version=CANARY_BENCH_VERSION,
                status="activated",
                cohort_size=5,
                created_at=now + timedelta(seconds=1),
                activated_at=now + timedelta(seconds=1),
            )
        )
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()


@pytest.mark.parametrize("capable_count", [0, 1, 2])
async def test_rollout_start_requires_one_capable_validator_and_matches_telemetry(
    session_maker: async_sessionmaker[AsyncSession],
    capable_count: int,
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    capabilities, stack = _capabilities(now)
    async with session_maker() as session, session.begin():
        for index in range(capable_count):
            session.add(
                ValidatorHeartbeat(
                    validator_hotkey=f"validator-{index}",
                    software_version="1.0.0",
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
        _add_ready_inference_route(session, now)
        await session.flush()

        telemetry = await rollout_state(session, now=now)
        assert telemetry["v3_capable_validator_count"] == capable_count
        if capable_count < 1:
            with pytest.raises(HTTPException) as exc_info:
                await _require_rollout_start_capacity(
                    session, now=now, desired_version=CANARY_BENCH_VERSION
                )
            assert exc_info.value.status_code == 409
            assert "requires at least 1" in str(exc_info.value.detail)
            assert await open_rollout(session) is None
        else:
            guarded = await _require_rollout_start_capacity(
                session, now=now, desired_version=CANARY_BENCH_VERSION
            )
            assert guarded["v3_capable_validator_count"] == capable_count


async def test_v7_rollout_start_requires_route_and_manifest_intersection(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    capabilities, stack = _capabilities(now)
    capabilities["scorer_benchmarks"]["v7_calibration"]["manifest_sha256"] = "d" * 64
    async with session_maker() as session, session.begin():
        session.add(
            ValidatorHeartbeat(
                validator_hotkey="validator-mismatched-manifest",
                software_version="1.0.0",
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
        _add_ready_inference_route(session, now)
        await session.flush()

        with pytest.raises(HTTPException) as exc_info:
            await _require_rollout_start_capacity(session, now=now, desired_version=7)
        assert exc_info.value.status_code == 409
        assert "exact route and manifest match" in str(exc_info.value.detail)


def _post_v7_capabilities(now: datetime) -> tuple[dict, dict]:
    capabilities, stack = _capabilities(now)
    scorer = capabilities["scorer_benchmarks"]
    scorer["supported_bench_versions"] = [8, 9, 10]
    scorer.pop("v7_calibration")
    return capabilities, stack


@pytest.mark.parametrize("bench_version", [8, 9, 10])
async def test_post_v7_rollout_uses_exact_route_without_retired_calibration(
    session_maker: async_sessionmaker[AsyncSession],
    bench_version: int,
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    model = benchmark_model(bench_version)
    profile_revision = aggregate_profile_revision(model, bench_version=bench_version)
    capabilities, stack = _post_v7_capabilities(now)
    requirements = InferenceActivationRequirements(
        enabled=True,
        provider_key_configured=True,
        model=model,
        routing_mode="aggregate_throughput",
        reviewed_manifest_sha256="c" * 64,
        aggregate_provider=AGGREGATE_PROVIDER,
        aggregate_profile_revision=profile_revision,
        aggregate_calibration_samples=AGGREGATE_CALIBRATION_SAMPLES,
    )
    async with session_maker() as session, session.begin():
        session.add(
            ValidatorHeartbeat(
                validator_hotkey="validator-v9-only",
                software_version="1.0.0",
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
        _add_ready_inference_route(
            session,
            now,
            provider=AGGREGATE_PROVIDER,
            profile_revision=profile_revision,
            calibrated=False,
        )
        await session.flush()

        guarded = await _require_rollout_start_capacity(
            session,
            now=now,
            desired_version=bench_version,
            routing_mode="aggregate_throughput",
            reviewed_manifest_sha256="c" * 64,
        )
        assert guarded["canary_capable_validator_count"] == 1
        assert await inference_activation_ready(
            session,
            bench_version=bench_version,
            now=now,
            requirements=requirements,
        )


async def test_v9_rollout_rejects_the_fixed_medium_v8_route(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    model = benchmark_model(9)
    capabilities, stack = _post_v7_capabilities(now)
    async with session_maker() as session, session.begin():
        session.add(
            ValidatorHeartbeat(
                validator_hotkey="validator-v9-wrong-route",
                software_version="1.0.0",
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
        _add_ready_inference_route(
            session,
            now,
            provider=AGGREGATE_PROVIDER,
            profile_revision=aggregate_profile_revision(model, bench_version=8),
        )
        await session.flush()

        with pytest.raises(HTTPException, match="exact inference route"):
            await _require_rollout_start_capacity(
                session,
                now=now,
                desired_version=9,
                routing_mode="aggregate_throughput",
                reviewed_manifest_sha256="c" * 64,
            )


def _heartbeat(
    hotkey: str, now: datetime, *, versions: list[int], protocol_version: int = 8
) -> ValidatorHeartbeat:
    capabilities, stack = _capabilities(now)
    capabilities["scorer_benchmarks"]["supported_bench_versions"] = versions
    if 7 not in versions:
        capabilities["scorer_benchmarks"].pop("v7_calibration", None)
        capabilities["ticket_inference"] = False
    return ValidatorHeartbeat(
        validator_hotkey=hotkey,
        software_version="1.0.0",
        protocol_version=protocol_version,
        code_digest="d" * 64,
        state="polling",
        first_seen_at=now,
        reported_at=now,
        seen_at=now,
        signature="ab" * 64,
        capabilities=capabilities,
        stack=stack,
    )


async def test_capability_gate_is_parameterised_per_bench_version() -> None:
    """A v4-capable heartbeat gates v4 in and v3 out, and vice versa."""
    now = datetime.now(UTC).replace(microsecond=0)
    v4_only = _heartbeat("v4-only", now, versions=[2, 4])
    v3_only = _heartbeat("v3-only", now, versions=[2, 3])

    assert heartbeat_supports_version(v4_only, now=now, version=4)
    assert not heartbeat_supports_version(v4_only, now=now, version=3)
    assert heartbeat_supports_version(v3_only, now=now, version=3)
    assert not heartbeat_supports_version(v3_only, now=now, version=4)
    # Both are gated by the same fixed protocol-8 wire floor.
    stale = _heartbeat("old", now, versions=[2, 4], protocol_version=7)
    assert not heartbeat_supports_version(stale, now=now, version=4)
    legacy_v7 = _heartbeat("legacy-v7", now, versions=[2, 7], protocol_version=10)
    assert not heartbeat_supports_version(legacy_v7, now=now, version=7)


@pytest.mark.parametrize(
    ("executor_isolation", "supports_v8"),
    [
        ("unknown", False),
        ("privileged_dind", True),
        ("rootless_dind", True),
        ("rootless_host", True),
        ("ephemeral_vm", True),
    ],
)
async def test_v8_requires_an_isolated_executor_but_v7_remains_compatible(
    executor_isolation: str, supports_v8: bool
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    heartbeat = _heartbeat(
        "executor-boundary", now, versions=[7, 8], protocol_version=18
    )
    capabilities = heartbeat.capabilities
    assert capabilities is not None
    capabilities["executor_isolation"] = executor_isolation

    assert heartbeat_supports_version(heartbeat, now=now, version=7)
    assert heartbeat_supports_version(heartbeat, now=now, version=8) is supports_v8


async def test_v8_only_scorer_does_not_require_retired_v7_calibration() -> None:
    """The v0.44 capability shape can receive v8 work without v7 metadata."""

    now = datetime.now(UTC).replace(microsecond=0)
    heartbeat = _heartbeat("v8-only", now, versions=[7, 8], protocol_version=18)
    capabilities = heartbeat.capabilities
    assert capabilities is not None
    scorer = capabilities["scorer_benchmarks"]
    scorer["supported_bench_versions"] = [8]
    scorer.pop("v7_calibration")

    assert heartbeat_supports_version(heartbeat, now=now, version=8)
    assert not heartbeat_supports_version(heartbeat, now=now, version=7)


@pytest.mark.parametrize(
    ("software_version", "supports_v9"),
    [
        ("0.51.2", False),
        ("source-build", False),
        ("0.51.9", False),
        ("0.52.0", False),
        ("0.53.0", False),
        ("0.53.1", False),
        ("0.53.2", False),
        ("0.53.4", False),
        ("0.53.5", False),
        ("0.53.6", False),
        ("0.53.7", False),
        ("0.53.8", False),
        ("0.53.9", False),
        ("0.53.10", True),
        ("0.54.0", True),
    ],
)
async def test_v9_requires_the_authoritative_enforce_scorer_release(
    software_version: str, supports_v9: bool
) -> None:
    """A stale v9 advertisement cannot make a broken embedding lane routable."""
    now = datetime.now(UTC).replace(microsecond=0)
    heartbeat = _heartbeat(
        f"scorer-{software_version}", now, versions=[7, 8, 9], protocol_version=18
    )
    capabilities = heartbeat.capabilities
    stack = heartbeat.stack
    assert capabilities is not None
    assert stack is not None
    capabilities["scorer_benchmarks"]["software_version"] = software_version
    stack["components"]["dittobench_api"]["version"] = software_version

    # The incident is v9-specific. Do not retire an otherwise valid v8 scorer.
    assert heartbeat_supports_version(heartbeat, now=now, version=8)
    assert heartbeat_supports_version(heartbeat, now=now, version=9) is supports_v9


async def test_v9_accepts_a_coherent_monorepo_source_build() -> None:
    """A self-managed release need not relabel its exact source-built scorer."""
    now = datetime.now(UTC).replace(microsecond=0)
    heartbeat = _heartbeat(
        "coherent-source-v9", now, versions=[7, 8, 9], protocol_version=18
    )
    capabilities = heartbeat.capabilities
    stack = heartbeat.stack
    assert capabilities is not None
    assert stack is not None
    revision = "c" * 40
    heartbeat.software_version = "0.53.23"
    scorer = capabilities["scorer_benchmarks"]
    scorer["software_version"] = "source-build"
    scorer["source_revision"] = revision
    stack["components"]["ditto_subnet"].update(
        source_revision=revision, version="0.53.23"
    )
    stack["components"]["dittobench_api"].update(
        source_revision=revision, version="source-build"
    )

    assert heartbeat_supports_version(heartbeat, now=now, version=9)


@pytest.mark.parametrize(
    ("worker_version", "stack_version", "worker_revision", "supports_v9"),
    [
        ("0.53.9", "0.53.9", "c" * 40, False),
        ("source-build", "source-build", "c" * 40, False),
        ("0.53.23", "0.53.22", "c" * 40, False),
        ("0.53.23", "0.53.23", "d" * 40, False),
        ("0.53.23", "0.53.23", "c" * 40, True),
    ],
)
async def test_v9_source_build_fallback_fails_closed_on_root_identity(
    worker_version: str,
    stack_version: str,
    worker_revision: str,
    supports_v9: bool,
) -> None:
    """The worker release substitutes only when every signed source id agrees."""
    now = datetime.now(UTC).replace(microsecond=0)
    heartbeat = _heartbeat(
        "source-v9-identity", now, versions=[7, 8, 9], protocol_version=18
    )
    capabilities = heartbeat.capabilities
    stack = heartbeat.stack
    assert capabilities is not None
    assert stack is not None
    revision = "c" * 40
    heartbeat.software_version = worker_version
    scorer = capabilities["scorer_benchmarks"]
    scorer["software_version"] = "source-build"
    scorer["source_revision"] = revision
    stack["components"]["ditto_subnet"].update(
        source_revision=worker_revision, version=stack_version
    )
    stack["components"]["dittobench_api"].update(
        source_revision=revision, version="source-build"
    )

    assert heartbeat_supports_version(heartbeat, now=now, version=9) is supports_v9


async def test_capable_counts_exclude_stale_v9_advertisers(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """A stale v9 advertisement cannot make a broken scorer routable."""
    now = datetime.now(UTC).replace(microsecond=0)
    async with session_maker() as session, session.begin():
        for index, software_version in enumerate(("0.53.9", "source-build", "0.53.10")):
            heartbeat = _heartbeat(
                f"v9-floor-{index}", now, versions=[7, 8, 9], protocol_version=18
            )
            assert heartbeat.capabilities is not None
            assert heartbeat.stack is not None
            heartbeat.capabilities["scorer_benchmarks"]["software_version"] = (
                software_version
            )
            heartbeat.stack["components"]["dittobench_api"]["version"] = (
                software_version
            )
            session.add(heartbeat)
        await session.flush()

        assert await capable_validator_counts(session, versions=[8, 9], now=now) == {
            8: 3,
            9: 1,
        }


async def test_capable_validator_counts_agree_with_the_per_version_state(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """The batched count is the same answer, from one heartbeat read.

    Target discovery needs this number for every shipped contract. It used to
    get it by running the full ``rollout_state`` derivation once per contract,
    so the two must stay identical or the cheap path is a different guarantee.
    """
    now = datetime.now(UTC).replace(microsecond=0)
    versions = [2, 3, 4, 5, 6]
    async with session_maker() as session, session.begin():
        session.add(_heartbeat("v4-only", now, versions=[2, 4]))
        session.add(_heartbeat("v3-only", now, versions=[2, 3]))
        session.add(_heartbeat("also-v4", now, versions=[2, 4]))
        await session.flush()

        batched = await capable_validator_counts(session, versions=versions, now=now)

        assert batched == {2: 3, 3: 1, 4: 2, 5: 0, 6: 0}
        for version in versions:
            state = await rollout_state(session, now=now, capability_version=version)
            assert batched[version] == state["canary_capable_validator_count"]


async def test_second_rollout_while_one_is_open_raises_conflict_not_integrity(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    async with session_maker() as session, session.begin():
        members, pins = await _seed_members(session, now)
        await create_rollout_snapshot(
            session,
            members=members,
            datasets=pins,
            now=now,
            from_version=7,
            desired_version=8,
        )
        with pytest.raises(RolloutConflictError) as exc_info:
            await create_rollout_snapshot(
                session,
                members=members,
                datasets=pins,
                now=now,
                from_version=7,
                desired_version=9,
            )
        assert "only one benchmark rollout may be open" in str(exc_info.value)


async def _seed_members(
    session, now: datetime
) -> tuple[list[RolloutSnapshotMember], dict[UUID, DatasetPin]]:
    members: list[RolloutSnapshotMember] = []
    pins: dict[UUID, DatasetPin] = {}
    for position in range(1, 6):
        agent_id = uuid4()
        session.add(
            Agent(
                agent_id=agent_id,
                miner_hotkey=f"miner-{position}",
                name=f"agent-{position}",
                sha256=f"{position:x}" * 64,
                status=AgentStatus.SCORED,
                screening_policy_version=9,
                screened_image_sha256=f"{position:x}" * 64,
                screened_image_size_bytes=1024,
                screened_image_id="sha256:" + f"{position:x}" * 64,
                screened_image_ref=f"ditto-screen/{agent_id}:latest",
                screened_image_upload_id=uuid4(),
                screened_image_verified_at=now,
                created_at=now + timedelta(seconds=position),
            )
        )
        members.append(
            RolloutSnapshotMember(
                agent_id=agent_id,
                miner_hotkey=f"miner-{position}",
                composite=1 - position / 100,
            )
        )
        pins[agent_id] = DatasetPin(seed=position, sha256="c" * 64, run_size="full")
    await session.flush()
    return members, pins


async def test_supersede_frees_the_open_slot_for_the_next_rollout(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    async with session_maker() as session, session.begin():
        members, pins = await _seed_members(session, now)
        stale = await create_rollout_snapshot(
            session,
            members=members,
            datasets=pins,
            now=now,
            from_version=7,
            desired_version=8,
        )
        assert await open_rollout(session) is not None

        superseded = await supersede_open_rollout(
            session, actor="nick", reason="v8 gate had false positives", now=now
        )
        assert superseded is not None
        assert superseded.rollout_id == stale.rollout_id
        assert superseded.status == "superseded"
        # The partial unique index excludes 'superseded', so the slot is free.
        assert await open_rollout(session) is None

        audit = (
            await session.scalars(
                select(BenchmarkRolloutAudit).where(
                    BenchmarkRolloutAudit.event == "superseded"
                )
            )
        ).all()
        assert len(audit) == 1
        assert audit[0].payload["actor"] == "nick"
        assert audit[0].payload["reason"] == "v8 gate had false positives"
        assert audit[0].payload["previous_status"] == "collecting"
        assert audit[0].payload["desired_version"] == 8

        # 7 -> 9 now inserts cleanly rather than tripping the unique index.
        fresh = await create_rollout_snapshot(
            session,
            members=members,
            datasets=pins,
            now=now + timedelta(seconds=1),
            from_version=7,
            desired_version=9,
        )
        assert fresh.desired_version == 9
        assert fresh.status == "collecting"
        await session.flush()


async def test_superseding_v8_rolls_the_target_back_to_active_v7(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    async with session_maker() as session, session.begin():
        session.add(
            BenchmarkRollout(
                rollout_id=uuid4(),
                from_version=6,
                desired_version=7,
                status="activated",
                cohort_size=5,
                created_at=now - timedelta(days=1),
                activated_at=now - timedelta(days=1),
            )
        )
        members, pins = await _seed_members(session, now)
        rollout = await create_rollout_snapshot(
            session,
            members=members,
            datasets=pins,
            now=now,
            from_version=7,
            desired_version=8,
        )
        assert await active_bench_version(session) == 7

        superseded = await supersede_open_rollout(
            session,
            actor="backroom:test",
            reason="roll back the unopened v8 target",
            now=now + timedelta(seconds=1),
        )
        assert superseded is not None
        assert superseded.rollout_id == rollout.rollout_id
        assert superseded.status == "superseded"
        assert await open_rollout(session) is None
        assert await active_bench_version(session) == 7


async def test_superseded_rollout_issues_no_tickets_and_never_activates(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    async with _seeded_rollout_session(session_maker, now) as (
        session,
        _agent_ids,
        rollout,
    ):
        assert await supersede_open_rollout(
            session, actor="nick", reason="abandoned", now=now
        )
        assert (
            await issue_rollout_ticket(
                session,
                validator_hotkey="validator-a",
                now=now,
                ttl=timedelta(minutes=90),
            )
            is None
        )
        assert not await maybe_activate_rollout(session, rollout, now=now)
        assert rollout.status == "superseded"
        assert await active_bench_version(session) == 2


async def test_supersede_refuses_rollout_after_priority_cohort_owns_authority(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    async with _seeded_session(
        session_maker, lambda s: _seed_desired_quorum_cohort(s, now)
    ) as (session, (_agent_ids, rollout)):
        assert await active_bench_version(session) == CANARY_BENCH_VERSION

        with pytest.raises(RolloutConflictError, match="already owns active authority"):
            await supersede_open_rollout(
                session,
                actor="operator",
                reason="must not roll authority backward",
                now=now,
            )

        assert rollout.status == "collecting"


async def test_operator_can_select_fully_qualified_superseded_authority(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    async with _seeded_session(
        session_maker, lambda s: _seed_desired_quorum_cohort(s, now)
    ) as (session, (_agent_ids, rollout)):
        rollout.status = "superseded"
        await session.flush()
        assert await active_bench_version(session) == 2

        selected = await select_active_bench_version(
            session,
            bench_version=CANARY_BENCH_VERSION,
            actor="operator",
            reason="restore the completed contract",
            now=now + timedelta(minutes=1),
            inference_requirements=_activation_requirements(),
        )

        assert selected.rollout_id == rollout.rollout_id
        assert selected.status == "superseded"
        assert await active_bench_version(session) == CANARY_BENCH_VERSION
        audit = await session.scalar(
            select(BenchmarkRolloutAudit).where(
                BenchmarkRolloutAudit.rollout_id == rollout.rollout_id,
                BenchmarkRolloutAudit.event == "authority_selected",
            )
        )
        assert audit is not None
        assert audit.payload["previous_active_version"] == 2
        assert audit.payload["bench_version"] == CANARY_BENCH_VERSION


async def test_operator_cannot_select_v7_after_proxy_key_rollback(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    async with _seeded_session(
        session_maker, lambda s: _seed_desired_quorum_cohort(s, now)
    ) as (session, (_agent_ids, rollout)):
        rollout.status = "superseded"
        await session.flush()

        with pytest.raises(
            RolloutConflictError,
            match="inference is not live on the exact reviewed route",
        ):
            await select_active_bench_version(
                session,
                bench_version=CANARY_BENCH_VERSION,
                actor="operator",
                reason="must remain fail closed",
                now=now + timedelta(minutes=1),
                inference_requirements=replace(
                    _activation_requirements(), provider_key_configured=False
                ),
            )

        assert await active_bench_version(session) == 2


async def test_activated_rollout_cannot_be_superseded(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    async with session_maker() as session, session.begin():
        session.add(
            BenchmarkRollout(
                rollout_id=uuid4(),
                from_version=7,
                desired_version=8,
                status="activated",
                cohort_size=5,
                created_at=now,
                activated_at=now,
            )
        )
        await session.flush()
        with pytest.raises(RolloutConflictError) as exc_info:
            await supersede_open_rollout(session, actor="nick", reason="oops", now=now)
        assert "activated" in str(exc_info.value)


async def test_admin_supersede_endpoint_audits_and_refuses_activated(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    async with session_maker() as session:
        # v6 has to be the era actually in force. ``supersede_open_rollout``
        # refuses a rollout that already owns active authority, and with no
        # activation on record the ledger answers the floor -- which is this
        # rollout's own target, so the supersede under test would 409 before it
        # ever ran. v6 held authority through an activated rollout now beneath
        # the floor; this is that grandfathered row.
        async with retired_era_writes_allowed(session), session.begin():
            await grandfather_active_era(
                session, version=6, now=now - timedelta(days=30)
            )
        async with session.begin():
            members, pins = await _seed_members(session, now)
            await create_rollout_snapshot(
                session,
                members=members,
                datasets=pins,
                now=now,
                # The admin route resolves the version against the shipped
                # contract registry, so this transition cannot move above v7
                # the way the pure-query supersede tests do -- v8 answers
                # "no shipped contract" and 409s before the supersede runs.
                from_version=6,
                desired_version=7,
            )
        # The path accepts the legacy "v7" spelling as well as a bare "7".
        state = await supersede_rollout(
            None,
            session,
            "v7",
            AdminRolloutSupersedeRequest(
                reason="false positives",
                actor="nick",
                confirmation="SUPERSEDE BENCHMARK V7",
            ),
        )
        assert state["status"] == "superseded"
        assert await open_rollout(session) is None

        # A second call has nothing open left to supersede.
        with pytest.raises(HTTPException) as exc_info:
            await supersede_rollout(
                None,
                session,
                "v7",
                AdminRolloutSupersedeRequest(
                    reason="again",
                    actor="nick",
                    confirmation="SUPERSEDE BENCHMARK V7",
                ),
            )
        assert exc_info.value.status_code == 409


async def test_admin_start_route_is_parameterised_by_version(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    async with session_maker() as session:
        async with session.begin():
            for index in range(2):
                session.add(_heartbeat(f"validator-{index}", now, versions=[2, 4]))
            await session.flush()
        # Telemetry is counted against the REQUESTED version, not a constant.
        assert (await get_rollout(None, session, "4"))[
            "canary_capable_validator_count"
        ] == 2
        assert (await get_rollout(None, session, "3"))[
            "canary_capable_validator_count"
        ] == 0

        # An unshipped version fails closed rather than opening a bad rollout.
        with pytest.raises(HTTPException) as exc_info:
            await get_rollout(None, session, "12")
        assert exc_info.value.status_code == 409
        with pytest.raises(HTTPException) as not_found:
            await get_rollout(None, session, "banana")
        assert not_found.value.status_code == 404


async def _seed_desired_quorum_cohort(
    session,
    now: datetime,
    *,
    desired_version: int = CANARY_BENCH_VERSION,
    smoke_indices: tuple[int, ...] = (),
    held_indices: tuple[int, ...] = (),
) -> tuple[list[UUID], BenchmarkRollout]:
    """The five-agent cohort, each member carrying a full raw desired quorum.

    ``smoke_indices`` gives those members a 3/3 quorum of sub-floor (smoke
    profile) runs — a quorum by row count that can never rank. ``held_indices``
    moves members out of the eligible pool.
    """
    agent_ids, rollout = await _seed_rollout(
        session, now, desired_version=desired_version
    )
    for position, agent_id in enumerate(agent_ids, start=1):
        smoke = (position - 1) in smoke_indices
        for validator in range(3):
            session.add(
                Score(
                    agent_id=agent_id,
                    bench_version=desired_version,
                    validator_hotkey=f"validator-{validator}",
                    run_id=f"v4-{position}-{validator}",
                    signature="bb",
                    seed=position,
                    composite=0.7 + position / 100,
                    tool_mean=0.7,
                    memory_mean=0.7,
                    median_ms=1,
                    n=50 if smoke else 114,
                    details={
                        "bench_version": desired_version,
                        "v9_base": {"semantic_gate_factor_bps": 10_000},
                    },
                    generated_at=now,
                )
            )
    for index in held_indices:
        agent = await session.get(Agent, agent_ids[index])
        assert agent is not None
        agent.status = AgentStatus.ATH_PENDING_REVIEW
    await session.flush()
    return agent_ids, rollout


async def _seed_non_member_ranked_agent(
    session: AsyncSession,
    *,
    now: datetime,
    desired_version: int,
) -> UUID:
    """A scored desired-version family that is not in the frozen snapshot."""
    agent_id = uuid4()
    session.add(
        Agent(
            agent_id=agent_id,
            miner_hotkey="miner-outsider-ranked",
            name="outsider",
            sha256="f" * 64,
            status=AgentStatus.SCORED,
            screening_policy_version=9,
            screened_image_sha256="f" * 64,
            screened_image_size_bytes=1024,
            screened_image_id="sha256:" + "f" * 64,
            screened_image_ref=f"ditto-screen/{agent_id}:latest",
            screened_image_upload_id=uuid4(),
            screened_image_verified_at=now,
            created_at=now + timedelta(hours=1),
        )
    )
    for validator in range(3):
        session.add(
            Score(
                agent_id=agent_id,
                bench_version=desired_version,
                validator_hotkey=f"outsider-validator-{validator}",
                run_id=f"outsider-{validator}",
                signature="cc",
                seed=900 + validator,
                composite=0.81,
                tool_mean=0.8,
                memory_mean=0.8,
                median_ms=1,
                n=114,
                details={"bench_version": desired_version},
                generated_at=now,
            )
        )
    await session.flush()
    return agent_id


async def _append_scoreless_rollout_tail(
    session: AsyncSession,
    *,
    rollout: BenchmarkRollout,
    now: datetime,
    suffix: str,
) -> UUID:
    agent_id = uuid4()
    session.add(
        Agent(
            agent_id=agent_id,
            miner_hotkey=f"miner-tail-{suffix}",
            name=f"tail-{suffix}",
            sha256="e" * 64,
            status=AgentStatus.SCORED,
            screening_policy_version=9,
            screened_image_sha256="e" * 64,
            screened_image_size_bytes=1024,
            screened_image_id="sha256:" + "e" * 64,
            screened_image_ref=f"ditto-screen/{agent_id}:latest",
            screened_image_upload_id=uuid4(),
            screened_image_verified_at=now,
            created_at=now + timedelta(minutes=rollout.cohort_size),
        )
    )
    rollout.cohort_size += 1
    assert await append_rollout_member(
        session,
        rollout=rollout,
        member=RolloutSnapshotMember(
            agent_id,
            f"miner-tail-{suffix}",
            0.1,
        ),
        dataset=DatasetPin(
            seed=rollout.cohort_size,
            sha256="e" * 64,
            run_size="full",
        ),
        now=now,
    )
    return agent_id


def _add_exhausted_tail_tickets(
    session: AsyncSession,
    *,
    agent_id: UUID,
    now: datetime,
    bench_version: int,
    running_validator: int | None = None,
) -> None:
    for validator in range(3):
        running = validator == running_validator
        session.add(
            ValidatorTicket(
                agent_id=agent_id,
                bench_version=bench_version,
                validator_hotkey=f"tail-validator-{validator}",
                status=(TicketStatus.ISSUED if running else TicketStatus.EXPIRED),
                purpose=TicketPurpose.CANONICAL_QUORUM,
                purpose_revision=2,
                issued_at=now - timedelta(minutes=10),
                deadline=now + timedelta(minutes=80) if running else now,
                attempt_count=2,
                manual_retry_grants=1,
                infra_retry_grants=0,
                failure_reason=None if running else "scoring_error",
                failed_at=None if running else now,
                failure_detail=None if running else "agent seed contract failed",
            )
        )


@pytest.mark.parametrize(
    ("smoke_indices", "held_indices"),
    [((0,), ()), ((), (0,))],
)
async def test_activation_requires_five_ranked_desired_quorum_agents(
    session_maker: async_sessionmaker[AsyncSession],
    smoke_indices: tuple[int, ...],
    held_indices: tuple[int, ...],
) -> None:
    # Activation is the last point the full-emission-set guarantee can be
    # enforced: afterwards open_rollout() is None, so list_eligible_ledger reads
    # the desired version unconditionally and its own threshold no longer
    # applies. rolling_top_five happens to refuse both degraded cohorts below on
    # its own today (COHORT_SIZE == MIN_DESIRED_AUTHORITY_AGENTS), so these
    # assert the guarantee, not which gate fired; the isolating case is
    # test_activation_refused_when_only_ranked_quorum_count_is_short.
    #
    # Parametrised rather than looped: each case needs a pristine database, which
    # the loop used to get by building a throwaway in-memory SQLite per iteration.
    now = datetime.now(UTC).replace(microsecond=0)
    async with _seeded_session(
        session_maker,
        lambda s: _seed_desired_quorum_cohort(
            s, now, smoke_indices=smoke_indices, held_indices=held_indices
        ),
    ) as (session, (_agent_ids, rollout)):
        # Raw row counts look like a complete cohort quorum.
        raw_counts = (
            await session.execute(
                select(Score.agent_id, func.count(Score.validator_hotkey))
                .where(Score.bench_version == CANARY_BENCH_VERSION)
                .group_by(Score.agent_id)
            )
        ).all()
        assert [count for _agent_id, count in raw_counts] == [3] * 5
        # Ranked, eligible quorums are what actually matter, and are short.
        assert (
            await count_ranked_quorum_agents(
                session, bench_version=CANARY_BENCH_VERSION
            )
            == MIN_DESIRED_AUTHORITY_AGENTS - 1
        )
        assert (
            await maybe_activate_rollout(
                session,
                rollout,
                now=now,
                inference_requirements=_activation_requirements(),
            )
            is False
        )
        assert rollout.status == "collecting"
        assert await active_bench_version(session) == 2


async def test_same_coldkey_generations_fill_one_rollout_position(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    async with _seeded_session(
        session_maker, lambda s: _seed_desired_quorum_cohort(s, now)
    ) as (session, (agent_ids, _rollout)):
        for index, agent_id in enumerate(agent_ids):
            agent = await session.get(Agent, agent_id)
            assert agent is not None
            session.add(
                EvaluationPayment(
                    block_hash=f"0x{agent_id.hex}",
                    extrinsic_index=0,
                    agent_id=agent_id,
                    miner_hotkey=agent.miner_hotkey,
                    miner_coldkey=(
                        "5SharedColdkey" if index < 2 else f"5Coldkey{index:048d}"
                    ),
                    amount_rao=1,
                    dest_address="5Destination",
                    timestamp=now,
                )
            )
        await session.flush()

        top = await rolling_top_five(session)

        assert len(top) == MIN_DESIRED_AUTHORITY_AGENTS - 1
        assert (
            await count_ranked_quorum_agents(
                session, bench_version=CANARY_BENCH_VERSION
            )
            == MIN_DESIRED_AUTHORITY_AGENTS - 1
        )


async def test_activation_refused_when_only_ranked_quorum_count_is_short(
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Isolates the new precondition: every legacy activation check is satisfied
    # (a converged five-member top five, a full raw quorum for every eligible
    # member) and only the ranked-quorum count is short. Without the
    # precondition this cohort would activate into a four-agent pool and the
    # KOTH tail would go short.
    now = datetime.now(UTC).replace(microsecond=0)
    async with _seeded_session(
        session_maker, lambda s: _seed_desired_quorum_cohort(s, now, smoke_indices=(0,))
    ) as (session, (agent_ids, rollout)):
        converged = [
            RolloutSnapshotMember(
                agent_id=agent_id, miner_hotkey=f"miner-{index + 1}", composite=0.9
            )
            for index, agent_id in enumerate(agent_ids)
        ]

        async def _converged_top_five(_session: object) -> list[RolloutSnapshotMember]:
            return converged

        monkeypatch.setattr(
            "ditto.db.queries.benchmark_rollout.rolling_top_five",
            _converged_top_five,
        )
        assert len(await rolling_top_five(session)) == MIN_DESIRED_AUTHORITY_AGENTS - 1
        assert (
            await maybe_activate_rollout(
                session,
                rollout,
                now=now,
                inference_requirements=_activation_requirements(),
            )
            is False
        )
        assert rollout.status == "collecting"


async def test_activation_at_five_ranked_quorums_keeps_a_full_emission_set(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    # The invariant that actually matters: it holds ACROSS the activation
    # boundary. Before, the ledger is pinned to v2 with five entries; after, it
    # is wholly on v4 and still has five.
    now = datetime.now(UTC).replace(microsecond=0)
    async with _seeded_session(
        session_maker, lambda s: _seed_desired_quorum_cohort(s, now)
    ) as (session, (agent_ids, rollout)):
        assert (
            await count_ranked_quorum_agents(
                session, bench_version=CANARY_BENCH_VERSION
            )
            == MIN_DESIRED_AUTHORITY_AGENTS
        )
        assert (
            await maybe_activate_rollout(
                session,
                rollout,
                now=now,
                inference_requirements=_activation_requirements(),
            )
            is True
        )
        assert rollout.status == "activated"
        assert await active_bench_version(session) == CANARY_BENCH_VERSION
        assert await open_rollout(session) is None

        ledger = await list_eligible_ledger(session)
        assert len(ledger) == MIN_DESIRED_AUTHORITY_AGENTS
        assert {row.agent_id for row in ledger} == set(agent_ids)
        assert {row.bench_version for row in ledger} == {CANARY_BENCH_VERSION}
        assert all(row.eligible for row in ledger)


async def test_activation_suppresses_three_terminal_scoreless_tail_members(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    async with _seeded_session(
        session_maker, lambda s: _seed_desired_quorum_cohort(s, now)
    ) as (session, (_agent_ids, rollout)):
        tail_ids = []
        for index in range(3):
            tail_id = await _append_scoreless_rollout_tail(
                session, rollout=rollout, now=now, suffix=f"terminal-{index}"
            )
            tail_ids.append(tail_id)
            _add_exhausted_tail_tickets(
                session,
                agent_id=tail_id,
                now=now,
                bench_version=rollout.desired_version,
            )
        await session.flush()

        assert await active_bench_version(session) == CANARY_BENCH_VERSION
        assert await maybe_activate_rollout(
            session,
            rollout,
            now=now,
            inference_requirements=_activation_requirements(),
        )
        assert rollout.status == "activated"
        assert await open_rollout(session) is None

        audits = list(
            await session.scalars(
                select(BenchmarkRolloutAudit)
                .where(BenchmarkRolloutAudit.rollout_id == rollout.rollout_id)
                .order_by(
                    BenchmarkRolloutAudit.recorded_at, BenchmarkRolloutAudit.audit_id
                )
            )
        )
        suppression = next(
            audit for audit in audits if audit.event == "tail_suppressed"
        )
        ordered_tail_ids = sorted(str(agent_id) for agent_id in tail_ids)
        assert suppression.payload == {
            "agent_ids": ordered_tail_ids,
            "maximum_agent_count": 3,
            "reason": "canonical retry budgets exhausted without a score",
        }
        activated = next(audit for audit in audits if audit.event == "activated")
        assert activated.payload["suppressed_tail_agent_ids"] == ordered_tail_ids
        assert all(
            activated.payload["score_counts"][str(tail_id)] == 0 for tail_id in tail_ids
        )


async def test_activation_does_not_suppress_a_running_tail_attempt(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    async with _seeded_session(
        session_maker, lambda s: _seed_desired_quorum_cohort(s, now)
    ) as (session, (_agent_ids, rollout)):
        tail_id = await _append_scoreless_rollout_tail(
            session, rollout=rollout, now=now, suffix="running"
        )
        _add_exhausted_tail_tickets(
            session,
            agent_id=tail_id,
            now=now,
            bench_version=rollout.desired_version,
            running_validator=0,
        )
        await session.flush()

        assert not await maybe_activate_rollout(
            session,
            rollout,
            now=now,
            inference_requirements=_activation_requirements(),
        )
        assert rollout.status == "collecting"


async def test_activation_does_not_suppress_multiple_unfinished_tail_members(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    async with _seeded_session(
        session_maker, lambda s: _seed_desired_quorum_cohort(s, now)
    ) as (session, (_agent_ids, rollout)):
        tail_id = await _append_scoreless_rollout_tail(
            session, rollout=rollout, now=now, suffix="terminal"
        )
        await _append_scoreless_rollout_tail(
            session, rollout=rollout, now=now, suffix="unfinished"
        )
        _add_exhausted_tail_tickets(
            session,
            agent_id=tail_id,
            now=now,
            bench_version=rollout.desired_version,
        )
        await session.flush()

        assert not await maybe_activate_rollout(
            session,
            rollout,
            now=now,
            inference_requirements=_activation_requirements(),
        )
        assert rollout.status == "collecting"


async def test_activation_does_not_suppress_more_than_three_exhausted_tail_members(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    async with _seeded_session(
        session_maker, lambda s: _seed_desired_quorum_cohort(s, now)
    ) as (session, (_agent_ids, rollout)):
        for index in range(4):
            tail_id = await _append_scoreless_rollout_tail(
                session, rollout=rollout, now=now, suffix=f"over-limit-{index}"
            )
            _add_exhausted_tail_tickets(
                session,
                agent_id=tail_id,
                now=now,
                bench_version=rollout.desired_version,
            )
        await session.flush()

        assert not await maybe_activate_rollout(
            session,
            rollout,
            now=now,
            inference_requirements=_activation_requirements(),
        )
        assert rollout.status == "collecting"


@pytest.mark.parametrize(
    ("failed_order_index", "expected_activation"),
    [(0, True), (1, False), (2, True)],
    ids=("lower-row-fails", "median-row-fails", "upper-row-fails"),
)
async def test_v9_activation_uses_median_semantic_evidence(
    session_maker: async_sessionmaker[AsyncSession],
    failed_order_index: int,
    expected_activation: bool,
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    async with _seeded_session(
        session_maker,
        lambda s: _seed_desired_quorum_cohort(s, now, desired_version=9),
    ) as (session, (agent_ids, rollout)):
        failed_agent_id = agent_ids[0]
        failed_rows = sorted(
            await session.scalars(
                select(Score).where(
                    Score.agent_id == failed_agent_id,
                    Score.bench_version == 9,
                )
            ),
            key=lambda score: score.composite,
        )
        assert len(failed_rows) == 3
        failed_score = failed_rows[failed_order_index]
        failed_score.details = {
            **(failed_score.details or {}),
            "v9_base": {"semantic_gate_factor_bps": 0},
        }
        await session.flush()

        assert (
            await count_ranked_quorum_agents(session, bench_version=9)
            == MIN_DESIRED_AUTHORITY_AGENTS
        )
        activated = await maybe_activate_rollout(
            session,
            rollout,
            now=now,
            inference_requirements=_activation_requirements(),
        )
        assert activated is expected_activation
        assert rollout.status == ("activated" if expected_activation else "collecting")
        assert await active_bench_version(session) == (9 if expected_activation else 2)


async def test_v9_activation_rejects_missing_semantic_evidence(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    async with _seeded_session(
        session_maker,
        lambda s: _seed_desired_quorum_cohort(s, now, desired_version=9),
    ) as (session, (agent_ids, rollout)):
        median_score = sorted(
            await session.scalars(
                select(Score).where(
                    Score.agent_id == agent_ids[0],
                    Score.bench_version == 9,
                )
            ),
            key=lambda score: score.composite,
        )[1]
        median_score.details = {"bench_version": 9}
        await session.flush()

        assert not await maybe_activate_rollout(
            session,
            rollout,
            now=now,
            inference_requirements=_activation_requirements(),
        )
        assert await active_bench_version(session) == 2


async def test_v9_failed_priority_member_does_not_deadlock_ready_tail_authority(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """A completed v9 rejection cannot veto a five-agent valid emission set."""
    now = datetime.now(UTC).replace(microsecond=0)
    async with _seeded_session(
        session_maker,
        lambda s: _seed_desired_quorum_cohort(s, now, desired_version=9),
    ) as (session, (agent_ids, rollout)):
        failed_id = agent_ids[0]
        failed_rows = list(
            await session.scalars(
                select(Score).where(
                    Score.agent_id == failed_id,
                    Score.bench_version == 9,
                )
            )
        )
        assert len(failed_rows) == 3
        for score in failed_rows:
            score.composite = 0
            score.details = {
                **(score.details or {}),
                "v9_base": {"semantic_gate_factor_bps": 0},
            }

        tail_id = uuid4()
        session.add(
            Agent(
                agent_id=tail_id,
                miner_hotkey="miner-tail",
                name="agent-tail",
                sha256="f" * 64,
                status=AgentStatus.SCORED,
                screening_policy_version=9,
                screened_image_sha256="f" * 64,
                screened_image_size_bytes=1024,
                screened_image_id="sha256:" + "f" * 64,
                screened_image_ref=f"ditto-screen/{tail_id}:latest",
                screened_image_upload_id=uuid4(),
                screened_image_verified_at=now,
                created_at=now + timedelta(minutes=1),
            )
        )
        rollout.cohort_size = 6
        assert await append_rollout_member(
            session,
            rollout=rollout,
            member=RolloutSnapshotMember(tail_id, "miner-tail", 0.4),
            dataset=DatasetPin(seed=6, sha256="f" * 64, run_size="full"),
            now=now,
        )
        unfinished_id = uuid4()
        session.add(
            Agent(
                agent_id=unfinished_id,
                miner_hotkey="miner-unfinished",
                name="agent-unfinished",
                sha256="e" * 64,
                status=AgentStatus.SCORED,
                screening_policy_version=9,
                screened_image_sha256="e" * 64,
                screened_image_size_bytes=1024,
                screened_image_id="sha256:" + "e" * 64,
                screened_image_ref=f"ditto-screen/{unfinished_id}:latest",
                screened_image_upload_id=uuid4(),
                screened_image_verified_at=now,
                created_at=now + timedelta(minutes=2),
            )
        )
        rollout.cohort_size = 7
        assert await append_rollout_member(
            session,
            rollout=rollout,
            member=RolloutSnapshotMember(unfinished_id, "miner-unfinished", 0.3),
            dataset=DatasetPin(seed=7, sha256="e" * 64, run_size="full"),
            now=now,
        )
        for validator in range(3):
            session.add(
                Score(
                    agent_id=tail_id,
                    bench_version=9,
                    validator_hotkey=f"validator-{validator}",
                    run_id=f"v4-tail-{validator}",
                    signature="bb",
                    seed=6,
                    composite=0.6,
                    tool_mean=0.6,
                    memory_mean=0.6,
                    median_ms=1,
                    n=114,
                    details={
                        "bench_version": 9,
                        "v9_base": {"semantic_gate_factor_bps": 10_000},
                    },
                    generated_at=now,
                )
            )
        await session.flush()

        assert (
            await count_ranked_quorum_agents(
                session,
                bench_version=9,
                agent_ids={*agent_ids, tail_id},
                require_v9_semantic_pass=True,
            )
            == MIN_DESIRED_AUTHORITY_AGENTS
        )
        # The frozen top-five gate is complete and a full desired-version
        # emission set exists, so authority flips without waiting for the
        # wider background-rescore cohort. The rollout remains open until that
        # tail finishes; no member is deleted or silently treated as complete.
        assert await active_bench_version(session) == 9
        collecting_ledger = await list_eligible_ledger(session)
        assert collecting_ledger
        assert {row.bench_version for row in collecting_ledger} == {9}
        assert unfinished_id not in {row.agent_id for row in collecting_ledger}
        assert not await maybe_activate_rollout(
            session,
            rollout,
            now=now,
            inference_requirements=_activation_requirements(),
        )

        # Completing the last member with a legitimate semantic failure closes
        # the durable rollout record without requiring that failure to rank.
        for validator in range(3):
            session.add(
                Score(
                    agent_id=unfinished_id,
                    bench_version=9,
                    validator_hotkey=f"validator-{validator}",
                    run_id=f"v4-unfinished-{validator}",
                    signature="cc",
                    seed=7,
                    composite=0,
                    tool_mean=0,
                    memory_mean=0,
                    median_ms=1,
                    n=114,
                    details={
                        "bench_version": 9,
                        "v9_base": {"semantic_gate_factor_bps": 0},
                    },
                    generated_at=now,
                )
            )
        await session.flush()

        assert await active_bench_version(session) == 9

        ledger = await list_eligible_ledger(session)
        assert {row.agent_id for row in ledger} == {
            *agent_ids,
            tail_id,
            unfinished_id,
        }
        assert {row.bench_version for row in ledger} == {9}
        by_agent = {row.agent_id: row for row in ledger}
        assert not by_agent[failed_id].eligible
        assert not by_agent[unfinished_id].eligible
        assert sum(row.eligible for row in ledger) == MIN_DESIRED_AUTHORITY_AGENTS

        assert await maybe_activate_rollout(
            session,
            rollout,
            now=now,
            inference_requirements=_activation_requirements(),
        )
        assert rollout.status == "activated"


async def test_activation_recovers_legacy_cohort_with_linked_priority_family(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """A malformed frozen cohort can finish without weakening either gate.

    Older rollout selection could put two attested-family members in the first
    five. The priority members must still each finish a ranked quorum, while a
    ranked independent tail member supplies the fifth emission-owner family.
    """
    now = datetime.now(UTC).replace(microsecond=0)
    async with _seeded_session(
        session_maker, lambda s: _seed_desired_quorum_cohort(s, now)
    ) as (session, (agent_ids, rollout)):
        tail_id = uuid4()
        session.add(
            Agent(
                agent_id=tail_id,
                miner_hotkey="miner-6",
                name="agent-6",
                sha256="6" * 64,
                status=AgentStatus.SCORED,
                screening_policy_version=9,
                created_at=now + timedelta(seconds=6),
            )
        )
        session.add(
            BenchmarkRolloutMember(
                rollout_id=rollout.rollout_id,
                agent_id=tail_id,
                position=6,
                frozen_miner_hotkey="miner-6",
                frozen_composite=0.94,
            )
        )
        rollout.cohort_size = 6
        for validator in range(3):
            session.add(
                Score(
                    agent_id=tail_id,
                    bench_version=CANARY_BENCH_VERSION,
                    validator_hotkey=f"validator-{validator}",
                    run_id=f"v4-6-{validator}",
                    signature="bb",
                    seed=6,
                    composite=0.76,
                    tool_mean=0.7,
                    memory_mean=0.7,
                    median_ms=1,
                    n=114,
                    details={
                        "bench_version": CANARY_BENCH_VERSION,
                        "v9_base": {"semantic_gate_factor_bps": 10_000},
                    },
                    generated_at=now,
                )
            )
        session.add(
            OwnerAttestation(
                netuid=expected_netuid(),
                hotkey_lo="miner-1",
                hotkey_hi="miner-2",
                nonce=uuid4(),
                issued_at=now,
                lo_key_kind="hotkey",
                lo_signer="miner-1",
                lo_signature="a" * 128,
                hi_key_kind="hotkey",
                hi_signer="miner-2",
                hi_signature="b" * 128,
            )
        )
        await session.flush()

        assert (
            await count_ranked_quorum_agents(
                session,
                bench_version=CANARY_BENCH_VERSION,
                agent_ids={*agent_ids, tail_id},
            )
            == MIN_DESIRED_AUTHORITY_AGENTS
        )
        state = await rollout_state(session, now=now)
        assert state["priority_complete"] is True
        assert state["ranked_quorum_agents"] == MIN_DESIRED_AUTHORITY_AGENTS
        assert await active_bench_version(session) == CANARY_BENCH_VERSION
        assert await maybe_activate_rollout(
            session,
            rollout,
            now=now,
            inference_requirements=_activation_requirements(),
        )
        assert rollout.status == "activated"


@pytest.mark.parametrize(
    ("requirements", "stale_route"),
    [
        (replace(_activation_requirements(), enabled=False), False),
        (
            replace(_activation_requirements(), provider_key_configured=False),
            False,
        ),
        (_activation_requirements(), True),
    ],
    ids=("proxy-disabled", "provider-key-removed", "stale-route"),
)
async def test_post_v7_top_five_authority_requires_live_inference_route(
    session_maker: async_sessionmaker[AsyncSession],
    requirements: InferenceActivationRequirements,
    stale_route: bool,
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    async with _seeded_session(
        session_maker, lambda s: _seed_desired_quorum_cohort(s, now)
    ) as (session, (_agent_ids, rollout)):
        if stale_route:
            route = await session.get(
                InferenceProviderRoute,
                (
                    "openai/gpt-oss-20b",
                    "Groq",
                    "openrouter-route-test-v1",
                ),
            )
            assert route is not None
            route.last_observed_at = now - timedelta(minutes=6)
        bind_inference_activation_requirements(session, requirements)
        assert await active_bench_version(session) == 2
        assert not await maybe_activate_rollout(
            session,
            rollout,
            now=now,
            inference_requirements=requirements,
        )
        assert rollout.status == "collecting"
        assert await persisted_active_bench_version(session) == 2


async def test_activation_skips_permanently_ineligible_frozen_members(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    async with _seeded_session(
        session_maker, lambda s: _seed_desired_quorum_cohort(s, now)
    ) as (session, (agent_ids, rollout)):
        outsider = await _seed_non_member_ranked_agent(
            session, now=now, desired_version=rollout.desired_version
        )
        agent = await session.get(Agent, agent_ids[0])
        assert agent is not None
        agent.status = AgentStatus.BANNED

        assert await maybe_activate_rollout(
            session,
            rollout,
            now=now,
            inference_requirements=_activation_requirements(),
        )
        assert rollout.status == "activated"
        assert rollout.blocked_reason is None
        assert await session.get(Agent, outsider) is not None


async def test_banning_frozen_member_does_not_revert_desired_authority(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    async with _seeded_session(
        session_maker, lambda s: _seed_desired_quorum_cohort(s, now)
    ) as (session, (agent_ids, rollout)):
        await _seed_non_member_ranked_agent(
            session, now=now, desired_version=rollout.desired_version
        )
        assert await active_bench_version(session) == rollout.desired_version

        agent = await session.get(Agent, agent_ids[0])
        assert agent is not None
        agent.status = AgentStatus.BANNED
        await session.flush()

        assert await active_bench_version(session) == rollout.desired_version
        assert await maybe_activate_rollout(
            session,
            rollout,
            now=now,
            inference_requirements=_activation_requirements(),
        )
        assert rollout.status == "activated"
        assert rollout.blocked_reason is None


async def test_rollout_state_active_version_matches_start_guard_authority(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """rollout_state's active_version must equal active_bench_version.

    Regression for the spurious "active benchmark changed: expected v9, found v8"
    409 on rollout start. The start guard compares the operator-supplied
    expected_active_version against active_bench_version(), while the operator UI
    reads it from rollout_state()["active_version"]. When those two derive the
    active version differently they disagree and start_rollout 409s even though
    nothing changed.

    The divergent state: an activated older transition plus a newer, terminally
    superseded transition that never activated (a real sequence -- a v9->v10 rollout
    opened while a converging v8->v9 briefly read as active, then v8->v9 was
    reverted, leaving v9->v10 dangling as the most-recent row). The most-recent row
    (superseded, from=9) and the latest activated row (desired=8) disagree; both
    reports must nonetheless agree with each other.
    """
    async with session_maker() as session:
        base = datetime(2026, 7, 1, tzinfo=UTC)
        # Older transition, activated: this is what the weight-setting guard treats
        # as authoritative (latest activated desired_version == 8).
        session.add(
            BenchmarkRollout(
                rollout_id=uuid4(),
                from_version=7,
                desired_version=8,
                status="activated",
                cohort_size=5,
                created_at=base,
                activated_at=base + timedelta(hours=1),
            )
        )
        # Newer transition, terminally superseded (never activated). Its from_version
        # is 9, so the pre-fix most-recent-row derivation reported active_version == 9.
        session.add(
            BenchmarkRollout(
                rollout_id=uuid4(),
                from_version=9,
                desired_version=10,
                status="superseded",
                cohort_size=5,
                created_at=base + timedelta(hours=2),
            )
        )
        await session.flush()

        guard = await active_bench_version(session)
        state = await rollout_state(session)
        assert guard == 8
        # The invariant the fix guarantees: the value the UI echoes back as
        # expected_active_version is exactly what the start guard checks.
        assert state["active_version"] == guard


async def _seed_eligible_v2_era(session, now: datetime, *, count: int) -> None:
    """``count`` distinctly-owned v2 agents any rollout could inherit."""
    # v2 is the era in force, which the operator start below asserts with
    # ``expected_active_version``. It only holds authority because an
    # activation says so; see ``grandfather_active_era``.
    await grandfather_active_era(
        session, version=DEFAULT_BENCH_VERSION, now=now - timedelta(days=30)
    )
    for position in range(1, count + 1):
        agent_id = uuid4()
        session.add(
            Agent(
                agent_id=agent_id,
                miner_hotkey=f"miner-cohort-{position}",
                name=f"cohort-{position}",
                sha256=f"{position:064x}",
                status=AgentStatus.SCORED,
                screening_policy_version=9,
                screened_image_sha256=f"{position:064x}",
                screened_image_size_bytes=1024,
                screened_image_id=f"sha256:{position:064x}",
                screened_image_ref=f"ditto-screen/{agent_id}:latest",
                screened_image_upload_id=uuid4(),
                screened_image_verified_at=now,
                dataset_seed=position,
                dataset_sha256="c" * 64,
                dataset_run_size="full",
                created_at=now + timedelta(seconds=position),
            )
        )
        for validator in range(3):
            session.add(
                Score(
                    agent_id=agent_id,
                    bench_version=2,
                    validator_hotkey=f"legacy-{validator}",
                    run_id=f"cohort-v2-{position}-{validator}",
                    signature="aa",
                    seed=position,
                    composite=1 - position / 1000,
                    tool_mean=0.5,
                    memory_mean=0.5,
                    median_ms=1,
                    n=114,
                    details={"bench_version": 2},
                    generated_at=now,
                )
            )
    capabilities, stack = _capabilities(now)
    session.add(
        ValidatorHeartbeat(
            validator_hotkey="validator-cohort",
            software_version="1.0.0",
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
    _add_ready_inference_route(session, now)


async def _start_for_cohort_size(
    session_maker: async_sessionmaker[AsyncSession], configured: int | None
) -> dict:
    """Start a rollout with 12 eligible inherited agents under a given policy."""
    now = datetime.now(UTC).replace(microsecond=0)
    async with (
        session_maker() as session,
        # The inherited era a rollout starts FROM is v2, which is beneath the
        # retired-era floor -- as it must be, since the only shipped target
        # above the floor is v7 and a rollout has to move forward into it. The
        # floor is restored on the way out, so the start below runs against it.
        retired_era_writes_allowed(session),
        session.begin(),
    ):
        await _seed_eligible_v2_era(session, now, count=12)
        if configured is not None:
            await insert_queue_policy_settings_revision(
                session,
                parent_revision=0,
                scope="*",
                settings={"rescore_cohort_size": configured},
                checksum="b" * 64,
                reason="the subnet is scaling; widen the rescore cohort",
                actor="peyton@omniaura.ai",
            )
    generator = AsyncMock()
    generator.generate.return_value = "e" * 64
    async with session_maker() as session:
        state = await start_rollout(
            None,
            session,
            generator,
            str(CANARY_BENCH_VERSION),
            AdminRolloutStartRequest(
                reason="operator opens shipped benchmark",
                actor="backroom:test",
                confirmation=f"START BENCHMARK V{CANARY_BENCH_VERSION}",
                expected_active_version=2,
            ),
        )
        await session.rollback()
        async with session.begin():
            rollout = await open_rollout(session)
            assert rollout is not None
            audit = await session.scalar(
                select(BenchmarkRolloutAudit).where(
                    BenchmarkRolloutAudit.rollout_id == rollout.rollout_id,
                    BenchmarkRolloutAudit.event == "cohort_frozen",
                )
            )
            assert audit is not None
            result = {
                "state": state,
                "target": rollout.rescore_cohort_target,
                "cohort_size": rollout.cohort_size,
                "audit_target": audit.payload["rescore_cohort_target"],
            }
    return result


async def test_rollout_start_defaults_to_the_inherited_top_ten(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    result = await _start_for_cohort_size(session_maker, None)
    assert result["target"] == 10
    assert result["cohort_size"] == 10
    assert result["audit_target"] == 10
    assert result["state"]["rescore_cohort_target"] == 10
    assert result["state"]["max_rescore_cohort_size"] == 25
    assert len(result["state"]["members"]) == 10


async def test_rollout_start_freezes_the_configured_cohort_size(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """The operator's 10 -> 25 change lands on the next start and is recorded."""
    result = await _start_for_cohort_size(session_maker, 25)
    assert result["target"] == 25
    # Only twelve inherited agents are eligible, so the cohort is what the two
    # prior eras could supply -- the target is the ceiling, not a quota.
    assert result["cohort_size"] == 12
    assert result["audit_target"] == 25
    assert result["state"]["rescore_cohort_target"] == 25
    assert len(result["state"]["members"]) == 12


async def test_rollout_start_honors_a_narrowed_cohort_size(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    result = await _start_for_cohort_size(session_maker, 5)
    assert result["target"] == 5
    assert result["cohort_size"] == 5
    assert len(result["state"]["members"]) == 5
